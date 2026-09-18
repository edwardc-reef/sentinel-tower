import factory
from factory.django import DjangoModelFactory

import apps.metagraph.models as metagraph_models


class ColdkeyFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.Coldkey
        django_get_or_create = ("coldkey",)

    coldkey = factory.Sequence(lambda n: f"5C{n:>062}")
    label = ""


class HotkeyFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.Hotkey
        django_get_or_create = ("hotkey",)

    hotkey = factory.Sequence(lambda n: f"5H{n:>062}")
    coldkey = factory.SubFactory(ColdkeyFactory)
    label = ""


class EvmKeyFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.EvmKey
        django_get_or_create = ("evm_address",)

    evm_address = factory.Sequence(lambda n: f"0x{n:>040x}")


class BlockFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.Block
        django_get_or_create = ("number",)

    number = factory.Sequence(lambda n: n + 1)
    timestamp = factory.Faker("date_time_this_year", tzinfo=None)


class SubnetFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.Subnet
        django_get_or_create = ("netuid",)

    netuid = factory.Sequence(lambda n: n + 1)
    name = factory.LazyAttribute(lambda o: f"subnet-{o.netuid}")
    alpha_out_emission = 0
    owner_cut = 0.09
    tempo = 360
    moving_price = 0.0


class NeuronFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.Neuron
        django_get_or_create = ("hotkey", "subnet")

    hotkey = factory.SubFactory(HotkeyFactory)
    subnet = factory.SubFactory(SubnetFactory)
    uid = factory.Sequence(lambda n: n)


class NeuronSnapshotFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.NeuronSnapshot

    neuron = factory.SubFactory(NeuronFactory)
    block = factory.SubFactory(BlockFactory)
    uid = factory.LazyAttribute(lambda o: o.neuron.uid)
    total_stake = 0
    normalized_stake = 0.0
    rank = 0.0
    trust = 0.0
    emissions = 0
    alpha_stake = 0
    alpha_dividends = 0
    tao_dividends = 0
    dividend_apy = 0.0
    is_active = False
    is_validator = False
    is_immune = False
    has_any_weights = False


class MechanismMetricsFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.MechanismMetrics

    snapshot = factory.SubFactory(NeuronSnapshotFactory)
    mech_id = 0
    incentive = 0.0
    dividend = 0.0
    consensus = 0.0
    validator_trust = 0.0
    weights_sum = 0.0


class WeightFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.Weight

    source_neuron = factory.SubFactory(NeuronFactory)
    target_neuron = factory.SubFactory(NeuronFactory)
    block = factory.SubFactory(BlockFactory)
    mech_id = 0
    weight = 0.0


class BondFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.Bond

    source_neuron = factory.SubFactory(NeuronFactory)
    target_neuron = factory.SubFactory(NeuronFactory)
    block = factory.SubFactory(BlockFactory)
    mech_id = 0
    bond = 0.0


class CollateralFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.Collateral

    source_neuron = factory.SubFactory(NeuronFactory)
    target_neuron = factory.SubFactory(NeuronFactory)
    block = factory.SubFactory(BlockFactory)
    amount = 0


class MetagraphDumpFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.MetagraphDump

    netuid = factory.Sequence(lambda n: n + 1)
    block = factory.SubFactory(BlockFactory)


class MetaEpochFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.MetaEpoch
        django_get_or_create = ("block",)

    block = factory.SubFactory(BlockFactory)


class SubnetBurnFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.SubnetBurn

    subnet = factory.SubFactory(SubnetFactory)
    meta_epoch = factory.SubFactory(MetaEpochFactory)
    source_block_number = factory.LazyAttribute(lambda o: o.meta_epoch.block_id)
    burn = 0.0
    superburn = 0.0


class SubnetEmissionFactory(DjangoModelFactory):
    class Meta:
        model = metagraph_models.SubnetEmission

    subnet = factory.SubFactory(SubnetFactory)
    meta_epoch = factory.SubFactory(MetaEpochFactory)
    emission_enabled = True
