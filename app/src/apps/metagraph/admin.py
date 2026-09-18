from django.contrib import admin

from .models import (
    Block,
    Bond,
    Coldkey,
    Collateral,
    EvmKey,
    Hotkey,
    MechanismMetrics,
    MetaEpoch,
    MetagraphDump,
    Neuron,
    NeuronSnapshot,
    Subnet,
    SubnetBurn,
    SubnetEmission,
    Weight,
)


@admin.register(Coldkey)
class ColdkeyAdmin(admin.ModelAdmin):
    list_display = ("id", "coldkey", "created_at")
    search_fields = ("coldkey",)
    list_filter = ("created_at",)
    readonly_fields = ("created_at",)


@admin.register(Hotkey)
class HotkeyAdmin(admin.ModelAdmin):
    list_display = ("id", "hotkey", "coldkey", "created_at", "last_seen")
    search_fields = ("hotkey", "coldkey__coldkey")
    list_filter = ("created_at", "last_seen")
    readonly_fields = ("created_at",)
    raw_id_fields = ("coldkey",)


@admin.register(EvmKey)
class EvmKeyAdmin(admin.ModelAdmin):
    list_display = ("id", "evm_address", "created_at")
    search_fields = ("evm_address",)
    list_filter = ("created_at",)
    readonly_fields = ("created_at",)


@admin.register(Block)
class BlockAdmin(admin.ModelAdmin):
    list_display = ("number", "timestamp", "dump_started_at", "dump_finished_at")
    search_fields = ("number",)
    list_filter = ("timestamp",)
    ordering = ("-number",)


@admin.register(Subnet)
class SubnetAdmin(admin.ModelAdmin):
    list_display = ("netuid", "name", "owner_hotkey", "registered_at")
    search_fields = ("netuid", "name", "owner_hotkey__hotkey")
    list_filter = ("registered_at",)
    raw_id_fields = ("owner_hotkey",)


@admin.register(Neuron)
class NeuronAdmin(admin.ModelAdmin):
    list_display = ("id", "uid", "subnet", "hotkey", "evm_key", "first_seen_block")
    search_fields = ("hotkey__hotkey", "evm_key__evm_address", "uid")
    list_filter = ("subnet",)
    raw_id_fields = ("hotkey", "subnet", "evm_key")


class MechanismMetricsInline(admin.TabularInline):
    model = MechanismMetrics
    extra = 0
    readonly_fields = (
        "mech_id",
        "incentive",
        "dividend",
        "consensus",
        "validator_trust",
        "weights_sum",
        "last_update",
    )


@admin.register(NeuronSnapshot)
class NeuronSnapshotAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "neuron",
        "block",
        "uid",
        "total_stake",
        "rank",
        "trust",
        "is_active",
        "is_validator",
    )
    search_fields = ("neuron__hotkey__hotkey", "uid")
    list_filter = ("is_active", "is_validator", "is_immune", "has_any_weights")
    raw_id_fields = ("neuron", "block")
    inlines = [MechanismMetricsInline]


@admin.register(MechanismMetrics)
class MechanismMetricsAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "snapshot",
        "mech_id",
        "incentive",
        "dividend",
        "consensus",
        "validator_trust",
    )
    search_fields = ("snapshot__neuron__hotkey__hotkey",)
    list_filter = ("mech_id",)
    raw_id_fields = ("snapshot",)


@admin.register(Weight)
class WeightAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source_neuron",
        "target_neuron",
        "block",
        "mech_id",
        "weight",
    )
    search_fields = (
        "source_neuron__hotkey__hotkey",
        "target_neuron__hotkey__hotkey",
    )
    list_filter = ("block", "mech_id")
    raw_id_fields = ("source_neuron", "target_neuron", "block")
    readonly_fields = ("created_at",)


@admin.register(Bond)
class BondAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source_neuron",
        "target_neuron",
        "block",
        "mech_id",
        "bond",
    )
    search_fields = (
        "source_neuron__hotkey__hotkey",
        "target_neuron__hotkey__hotkey",
    )
    list_filter = ("block", "mech_id")
    raw_id_fields = ("source_neuron", "target_neuron", "block")
    readonly_fields = ("created_at",)


@admin.register(Collateral)
class CollateralAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "source_neuron",
        "target_neuron",
        "block",
        "amount",
    )
    search_fields = (
        "source_neuron__hotkey__hotkey",
        "target_neuron__hotkey__hotkey",
    )
    list_filter = ("block",)
    raw_id_fields = ("source_neuron", "target_neuron", "block")
    readonly_fields = ("created_at",)


@admin.register(MetagraphDump)
class MetagraphDumpAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "netuid",
        "block",
        "owner_hotkey",
        "epoch_position",
        "started_at",
        "finished_at",
    )
    search_fields = ("netuid", "owner_hotkey__hotkey")
    list_filter = ("netuid", "block")
    raw_id_fields = ("block", "owner_hotkey")
    readonly_fields = ("created_at",)


@admin.register(MetaEpoch)
class MetaEpochAdmin(admin.ModelAdmin):
    list_display = ("block", "block_timestamp")
    search_fields = ("block__number",)
    raw_id_fields = ("block",)
    ordering = ("-block__number",)

    @admin.display(description="Timestamp", ordering="block__timestamp")
    def block_timestamp(self, obj: MetaEpoch):
        return obj.block.timestamp


@admin.register(SubnetBurn)
class SubnetBurnAdmin(admin.ModelAdmin):
    list_display = ("id", "subnet", "meta_epoch", "source_block_number", "burn", "superburn")
    search_fields = ("subnet__netuid", "source_block_number")
    list_filter = ("subnet",)
    raw_id_fields = ("subnet", "meta_epoch")
    ordering = ("-meta_epoch", "subnet")


@admin.register(SubnetEmission)
class SubnetEmissionAdmin(admin.ModelAdmin):
    list_display = ("id", "subnet", "meta_epoch", "emission_enabled")
    search_fields = ("subnet__netuid",)
    list_filter = ("emission_enabled", "subnet")
    raw_id_fields = ("subnet", "meta_epoch")
    ordering = ("-meta_epoch", "subnet")
