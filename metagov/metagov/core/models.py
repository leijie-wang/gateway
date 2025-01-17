import logging
import time
import uuid
from enum import Enum

import jsonpickle
import jsonschema
import requests
from django.conf import settings
from django.db import IntegrityError, models

from django.utils.translation import gettext_lazy as _
from metagov.core.plugin_manager import Parameters, plugin_registry

logger = logging.getLogger(__name__)


class AuthType:
    API_KEY = "api_key"
    OAUTH = "oauth"
    NONE = "none"


class Community(models.Model):
    slug = models.SlugField(
        max_length=36, default=uuid.uuid4, unique=True, help_text="Unique slug identifier for the community"
    )
    readable_name = models.CharField(max_length=100, blank=True, help_text="Human-readable name for the community")

    def __str__(self):
        if self.readable_name:
            return f"{self.readable_name} ({self.slug})"
        return str(self.slug)

    @property
    def plugins(self):
        return Plugin.objects.filter(community=self)

    def get_plugin(self, plugin_name, community_platform_id=None, id=None):
        """Get plugin proxy instance"""

        cls = plugin_registry.get(plugin_name)
        if not cls:
            raise ValueError(f"Plugin '{plugin_name}' not found")
        if id is not None:
            return cls.objects.get(name=plugin_name, community=self, pk=id)
        if community_platform_id:
            return cls.objects.get(name=plugin_name, community=self, community_platform_id=community_platform_id)
        else:
            return cls.objects.get(name=plugin_name, community=self)

    def enable_plugin(self, plugin_name, plugin_config=None):
        """Enable or update plugin"""
        from metagov.core.utils import create_or_update_plugin

        plugin, created = create_or_update_plugin(plugin_name, plugin_config or {}, self)
        return plugin

    def disable_plugin(self, plugin_name, community_platform_id=None, id=None):
        """Disable plugin"""
        plugin = self.get_plugin(plugin_name, community_platform_id, id=id)
        logger.debug(f"Disabling plugin '{plugin}'")
        plugin.delete()

    def perform_action(
        self, plugin_name, action_id, parameters=None, jsonschema_validation=True, community_platform_id=None
    ):
        """Perform an action in the community"""
        # Look up plugin instance
        cls = plugin_registry[plugin_name]
        meta = cls._action_registry[action_id]
        plugin = self.get_plugin(plugin_name, community_platform_id)

        # Validate input parameters
        if jsonschema_validation and meta.input_schema and parameters:
            jsonschema.validate(parameters, meta.input_schema)

        # Invoke action function
        action_function = getattr(plugin, meta.function_name)
        result = action_function(**parameters or {})

        # Validate result
        if jsonschema_validation and meta.output_schema and result:
            jsonschema.validate(result, meta.output_schema)

        return result


class DataStore(models.Model):
    datastore = models.JSONField(default=dict)

    def get(self, key):
        value = self.datastore.get(key)
        if value is not None:
            return jsonpickle.decode(value)
        return value

    def set(self, key, value):
        self.datastore[key] = jsonpickle.encode(value)
        self.save()
        return True

    def remove(self, key):
        res = self.datastore.pop(key, None)
        self.save()
        if not res:
            return False
        return True


class PluginManager(models.Manager):
    def get_queryset(self):
        qs = super(PluginManager, self).get_queryset()
        if self.model._meta.proxy:
            # this is a proxy model, so only return plugins of this proxy type
            return qs.filter(name=self.model.name)
        return qs


class LinkType(Enum):
    OAUTH = "oauth"
    MANUAL_ADMIN = "manual admin"
    EMAIL_MATCHING = "email matching"
    UNKNOWN = "unknown"


class LinkQuality(Enum):
    STRONG_CONFIRM = "confirmed (strong)"
    WEAK_CONFIRM = "confirmed (weak)"
    UNCONFIRMED = "unconfirmed"
    UNKNOWN = "unknown"


def quality_is_greater(a, b):
    order = [
        LinkQuality.UNKNOWN.value,
        LinkQuality.UNCONFIRMED.value,
        LinkQuality.WEAK_CONFIRM.value,
        LinkQuality.STRONG_CONFIRM.value,
    ]
    return order.index(a) > order.index(b)


class Plugin(models.Model ):
    """Represents an instance of an activated plugin."""

    name = models.CharField(max_length=30, blank=True, help_text="Name of the plugin")
    community = models.ForeignKey(
        Community, models.CASCADE, related_name="plugins", help_text="Community that this plugin instance belongs to"
    )
    config = models.JSONField(default=dict, null=True, blank=True, help_text="Configuration for this plugin instance")
    community_platform_id = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Optional identifier for this instance. If multiple instances are allowed per community, this field must be set to a unique value for each instance.",
    )
    state = models.OneToOneField(DataStore, models.CASCADE, help_text="Datastore to persist any state", null=True)

    # Static metadata
    auth_type = AuthType.NONE
    """If this plugin makes authenticated requests to an external platform, this field declares how the authentication occurs (API key or OAUTH). (Optional)"""

    config_schema = {}
    """JSON schema for the config object. If set, config will be validated against the schema when the plugin is enabled. (Optional)"""

    community_platform_id_key = None
    """Key on the config that represents a unique community identifier on the platform. If set, this config value will be automatically used as the 'community_platform_id.' (Optional)"""

    objects = PluginManager()

    class Meta:
        unique_together = ["name", "community", "community_platform_id"]

    def __str__(self):
        community_platform_id_str = ""
        if self.community_platform_id:
            community_platform_id_str = f" ({self.community_platform_id})"
        return f"{self.name}{community_platform_id_str} for '{self.community}'"

    def save(self, *args, **kwargs):
        if not self.pk:
            self.state = DataStore.objects.create()
        super(Plugin, self).save(*args, **kwargs)

    def initialize(self):
        """Initialize the plugin. Invoked once, directly after the plugin instance is created."""
        pass

    def start_process(self, process_name, callback_url=None, **kwargs):
        """Start a new GovernanceProcess"""
        # Find the proxy class for the specified GovernanceProcess
        cls = self.__get_process_cls(process_name)
       
        # Convert kwargs to Parameters (does schema validation and filling in default values)
        params = Parameters(values=kwargs, schema=cls.input_schema)
        # Create new process instance
        new_process = cls.objects.create(name=process_name, callback_url=callback_url, plugin=self)
        logger.debug(f"Created process: {new_process}")

        # Start process
        try:
            new_process.start(params)
        except Exception as e:
            # Delete model if any exceptions were raised
            new_process.delete()
            raise e

        logger.debug(f"Started process: {new_process}")
        return new_process

    def __get_process_cls(self, process_name):
        processes = plugin_registry[self.name]._process_registry
        if process_name not in processes:
            raise ValueError(
                f"No such process '{process_name}' for {self.name} plugin. Available processes: {list(processes.keys())}"
            )
        return processes[process_name]

    def get_processes(self, process_name):
        cls = self.__get_process_cls(process_name)
        return cls.objects.all()

    def get_process(self, id):
        process = GovernanceProcess.objects.get(pk=id)
        cls = self.__get_process_cls(process.name)
        return cls.objects.get(pk=id)

    def send_event_to_driver(self, event_type: str, data: dict, initiator: dict):
        """Send an event to the driver"""
        event = {
            "community": self.community.slug,
            "source": self.name,
            "event_type": event_type,
            "timestamp": str(time.time()),
            "data": data,
            "initiator": initiator,
        }

        # Emit signal
        from metagov.core.signals import platform_event_created

        platform_event_created.send(sender=self.__class__, instance=self, **event)

        # Post serialized event to receiver HTTP endpoint
        # TODO: maybe move this into a receiver for the platform_event_created signal?
        if getattr(settings, "DRIVER_EVENT_RECEIVER_URL", None):
            serialized = jsonpickle.encode(event, unpicklable=False)
            logger.debug("Sending event to Driver: " + serialized)
            resp = requests.post(settings.DRIVER_EVENT_RECEIVER_URL, data=serialized)
            if not resp.ok:
                logger.error(
                    f"Error sending event to driver at {settings.DRIVER_EVENT_RECEIVER_URL}: {resp.status_code} {resp.reason}"
                )

    def add_linked_account(
        self, *, platform_identifier, external_id=None, custom_data=None, link_type=None, link_quality=None
    ):
        """Given a platform identifier, creates or updates a linked account. Also creates a metagov
        id for the user if no external_id is passed in.
        """
        from metagov.core import identity

        optional_params = {
            "community_platform_id": self.community_platform_id,
            "custom_data": custom_data,
            "link_type": link_type,
            "link_quality": link_quality,
        }
        optional_params = identity.strip_null_values_from_dict(optional_params)

        try:
            # if linked account exists, update if new data is higher quality
            result = identity.retrieve_account(
                self.community, self.name, platform_identifier, self.community_platform_id
            )
            if link_quality and quality_is_greater(link_quality, result.link_quality):
                result = identity.update_linked_account(
                    self.community, self.name, platform_identifier, self.community_platform_id, **optional_params
                )

        except ValueError as error:
            # otherwise create linked account
            if not external_id:
                external_id = identity.create_id(self.community)[0]
            result = identity.link_account(
                external_id, self.community, self.name, platform_identifier, **optional_params
            )

        return result

    def serialize(self):
        from metagov.core.serializers import PluginSerializer

        return PluginSerializer(self).data


class ProcessStatus(Enum):
    CREATED = "created"
    PENDING = "pending"
    COMPLETED = "completed"


class GovernanceProcessManager(models.Manager):
    def get_queryset(self):
        qs = super(GovernanceProcessManager, self).get_queryset()
        if self.model._meta.proxy:
            # this is a proxy model, so only return processes of this proxy type
            return qs.filter(name=self.model.name, plugin__name=self.model.plugin_name)
        return qs


class GovernanceProcess(models.Model):
    """Represents an instance of a governance process."""

    name = models.CharField(max_length=30)
    url = models.CharField(max_length=150, null=True, blank=True, help_text="URL of the vote or process")
    callback_url = models.CharField(
        max_length=100, null=True, blank=True, help_text="Callback URL to notify when the process is updated"
    )
    status = models.CharField(
        max_length=15, choices=[(s.value, s.name) for s in ProcessStatus], default=ProcessStatus.CREATED.value
    )
    plugin = models.ForeignKey(
        Plugin, models.CASCADE, related_name="plugin", help_text="Plugin instance that this process belongs to"
    )
    state = models.OneToOneField(
        DataStore, models.CASCADE, help_text="Datastore to persist any internal state", null=True
    )
    errors = models.JSONField(default=dict, blank=True, help_text="Errors to serialize and send back to driver")
    outcome = models.JSONField(default=dict, blank=True, help_text="Outcome to serialize and send back to driver")

    # Optional: description of the governance process
    description = None
    # Optional: JSONSchema for start parameters object
    input_schema = None
    # Optional: JSONSchema for outcome object
    outcome_schema = None

    objects = GovernanceProcessManager()

    def __str__(self):
        return f"{self.plugin.name}.{self.name} for '{self.plugin.community.slug}' ({self.pk}, {self.status})"

    def save(self, *args, **kwargs):
        if not self.pk:
            self.state = DataStore.objects.create()
        super(GovernanceProcess, self).save(*args, **kwargs)

    def start(self, parameters):
        """(REQUIRED) Start the governance process.

        Most implementations of this function will:

        - Make a request to start a governance process in an external system

        - Store any data in ``outcome`` that should be returned to the Driver. For example, the URL for a voting process in another system.

        - Store any internal state in ``state``

        - If process was started successfully, set ``status`` to ``pending``.

        - If process failed to start, raise an exception of type ``PluginErrorInternal``.

        - Call ``self.save()`` to persist changes."""
        pass

    def close(self):
        """(OPTIONAL) Close the governance process.

        Most implementations of this function will:

        - Make a request to close the governance process in an external system

        - If the process was closed successfully, set ``status`` to ``completed`` and set the ``outcome``.

        - If the process failed to close, set ``errors`` or raise an exception of type ``PluginErrorInternal``.

        - Call ``self.save()`` to persist changes.
        """
        raise NotImplementedError

    def receive_webhook(self, request):
        """(OPTIONAL) Receive an incoming webhook from an external system. This is the preferred way to update the process state.

        Most implementations of this function will:

        - Check if the webhook request pertains to this process.

        - Update ``state``, ``status``, ``outcome``, and/or ``errors`` as needed.

        - Call ``self.save()`` to persist changes."""
        pass

    def update(self):
        """(OPTIONAL) Update the process outcome. This function will be invoked repeatedly from a scheduled task. It's only necessary to implement
        this function if you can't use webhooks to update the process state.

        Implementations of this function might:

        - Make a request to get the current status from an external system. OR,

        - Check if a closing condition has has been met. For example, if a voting process should be closed after a specified amount of time.

        - Update ``state``, ``status``, ``outcome``, and/or ``errors`` as needed.

        - Call ``self.save()`` to persist changes."""
        pass

    @property
    def proxy(self):
        # TODO: can we do this without hitting the database?
        cls = plugin_registry[self.plugin.name]._process_registry[self.name]
        return cls.objects.get(pk=self.pk)

class MetagovID(models.Model):
    """Metagov ID table links all public_ids to a single internal representation of a user. When data
    associated with public_ids conflicts, primary_ID is used.

    Fields:

    community: foreign key - metagov community the user is part of
    internal_id: integer - unique, secret ID
    external_id: integer - unique, public ID
    linked_ids: many2many - metagovIDs that a given ID has been merged with
    primary: boolean - used to resolve conflicts between linked MetagovIDs."""

    community = models.ForeignKey(Community, on_delete=models.CASCADE)
    internal_id = models.PositiveIntegerField(unique=True)
    external_id = models.PositiveIntegerField(unique=True)
    linked_ids = models.ManyToManyField("self")
    primary = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        """Performs extra validation on save such that if there are linked IDs, only one should have primary
        set as True. Only runs on existing instance."""
        if self.pk and self.linked_ids.all():
            true_count = sum([self.primary] + [linked_id.primary for linked_id in self.linked_ids.all()])
            if true_count == 0:
                raise IntegrityError("At least one linked ID must have 'primary' attribute set to True.")
            if true_count > 1:
                raise IntegrityError("More than one linked ID has 'primary' attribute set to True.")
        super(MetagovID, self).save(*args, **kwargs)

    def is_primary(self):
        """Helper method to determine if a MetagovID is primary. Accounts for the fact that a MetagovID
        with no linked IDs is primary, even if its primary attribute is set to False."""
        if self.primary or len(self.linked_ids.all()) == 0:
            return True
        return False

    def get_primary_id(self):
        """Helper method to restore the primary MetagovID for this user, whether it's the called
        instance of a linked_instance."""
        if self.is_primary():
            return self
        for linked_id in self.linked_ids.all():
            if linked_id.is_primary():
                return linked_id
        raise ValueError(f"No primary ID associated with {self.external_id}")


class LinkedAccount(models.Model):
    """Contains information about specific platform account linked to user

    Fields:

    metagov_id: foreign key to MetagovID
    community: foreign key - metagov community the user is part of
    community_platform_id: string (optional) - distinguishes between ie two Slacks in the same community
    platform_type: string - ie Github, Slack
    platform_identifier: string - ID, username, etc, unique to the platform (or unique to community_platform_id)
    custom_data: dict- optional additional data for linked platform account
    link_type: string (choice) - method through which account was linked
    link_quality: string (choice) - metagov's assessment of the quality of the link (depends on method)
    """

    metagov_id = models.ForeignKey(MetagovID, on_delete=models.CASCADE, related_name="linked_accounts")
    community = models.ForeignKey(Community, on_delete=models.CASCADE)
    community_platform_id = models.CharField(max_length=100, blank=True, null=True)
    platform_type = models.CharField(max_length=50)
    platform_identifier = models.CharField(max_length=200)

    custom_data = models.JSONField(default=dict)
    link_type = models.CharField(
        max_length=30, choices=[(t.value, t.name) for t in LinkType], default=LinkType.UNKNOWN.value
    )
    link_quality = models.CharField(
        max_length=30, choices=[(q.value, q.name) for q in LinkQuality], default=LinkQuality.UNKNOWN.value
    )

    def save(self, *args, **kwargs):
        """Performs extra validation on save such that community, platform type, identifier, and community_platform_id
        are unique together."""
        result = LinkedAccount.objects.filter(
            community=self.community,
            platform_type=self.platform_type,
            platform_identifier=self.platform_identifier,
            community_platform_id=self.community_platform_id,
        )
        if (not self.pk and result) or (self.pk and result and result[0].pk != self.pk):
            raise IntegrityError(
                f"LinkedAccount with the following already exists: community {self.community};"
                f"platform_type: {self.platform_type}; platform_identifier: {self.platform_identifier}"
                f"community_platform_id: {self.community_platform_id}"
            )
        super(LinkedAccount, self).save(*args, **kwargs)

    def serialize(self):
        return {
            "external_id": self.metagov_id.external_id,
            "community": self.community.slug,
            "community_platform_id": self.community_platform_id,
            "platform_type": self.platform_type,
            "platform_identifier": self.platform_identifier,
            "custom_data": self.custom_data,
            "link_type": self.link_type,
            "link_quality": self.link_quality,
        }
