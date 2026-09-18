"""Tests for data migrations whose effect outlives the schema change itself."""

import importlib

import pytest
from django.apps import apps as django_apps

from apps.metagraph.models import MetagraphDump
from tests.factories.metagraph import (
    ColdkeyFactory,
    HotkeyFactory,
    MetagraphDumpFactory,
    SubnetFactory,
)

stamp_current_owner_on_existing_dumps = importlib.import_module(
    "apps.metagraph.migrations.0016_burn_and_subnet_emission"
).stamp_current_owner_on_existing_dumps


@pytest.mark.django_db
def test_dumps_taken_before_the_column_existed_inherit_the_current_owner():
    """Without a stamp those dumps would score as zero burn for their whole history."""
    owner_hotkey = HotkeyFactory(coldkey=ColdkeyFactory(coldkey="5CurrentOwner"))
    SubnetFactory(netuid=7, owner_hotkey=owner_hotkey)
    SubnetFactory(netuid=8, owner_hotkey=None)
    MetagraphDumpFactory(netuid=7, owner_hotkey=None)
    MetagraphDumpFactory(netuid=8, owner_hotkey=None)

    stamp_current_owner_on_existing_dumps(django_apps, None)

    stamped = dict(MetagraphDump.objects.values_list("netuid", "owner_hotkey__hotkey"))
    assert stamped == {7: owner_hotkey.hotkey, 8: None}


@pytest.mark.django_db
def test_an_owner_already_recorded_on_a_dump_is_left_alone():
    """Re-running the migration must not overwrite an owner read from the chain."""
    dumped_owner = HotkeyFactory(coldkey=ColdkeyFactory(coldkey="5OwnerAtTheBlock"))
    SubnetFactory(netuid=7, owner_hotkey=HotkeyFactory(coldkey=ColdkeyFactory(coldkey="5CurrentOwner")))
    MetagraphDumpFactory(netuid=7, owner_hotkey=dumped_owner)

    stamp_current_owner_on_existing_dumps(django_apps, None)

    assert MetagraphDump.objects.get().owner_hotkey == dumped_owner
