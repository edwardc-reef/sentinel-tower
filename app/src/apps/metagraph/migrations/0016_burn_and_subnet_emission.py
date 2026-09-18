"""Burn, superburn, and subnet-emission caches, plus the per-dump owner they read.

Burn is recomputed from historical dumps (``backfill_burn_metrics``), so it needs
the owner as of the dumped block rather than the mutable ``Subnet.owner_hotkey``.

Dumps taken before this column existed carry no owner of their own. Leaving them
null would score their whole history as zero burn, so they are stamped with the
subnet's current owner — the same identity a subnet-row lookup would have given
them, except now frozen, so a *later* ownership change no longer rewrites them.
Ownership changes that already happened stay unrecoverable from stored data;
re-running ``historical_metagraph_backfill`` over the range re-reads the true
owner from the archive and overwrites the stamp.
"""

import django.core.validators
import django.db.models.deletion
from django.db import migrations, models


def stamp_current_owner_on_existing_dumps(apps, schema_editor):
    subnet_model = apps.get_model("metagraph", "Subnet")
    dump_model = apps.get_model("metagraph", "MetagraphDump")

    owners = subnet_model.objects.exclude(owner_hotkey=None).values_list("netuid", "owner_hotkey_id")
    for netuid, owner_hotkey_id in owners:
        # One indexed UPDATE per subnet (the unique (netuid, block) index covers
        # the lookup); subnets number in the hundreds, dumps in the millions.
        dump_model.objects.filter(netuid=netuid, owner_hotkey__isnull=True).update(owner_hotkey_id=owner_hotkey_id)


class Migration(migrations.Migration):
    dependencies = [
        ("metagraph", "0015_validator_apy_epoch"),
    ]

    operations = [
        migrations.CreateModel(
            name="MetaEpoch",
            fields=[
                (
                    "block",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="meta_epoch",
                        serialize=False,
                        to="metagraph.block",
                    ),
                ),
            ],
            options={
                "verbose_name": "meta epoch",
                "verbose_name_plural": "meta epochs",
                "db_table": "metagraph_meta_epoch",
            },
        ),
        migrations.CreateModel(
            name="SubnetBurn",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "source_block_number",
                    models.PositiveBigIntegerField(help_text="Subnet epoch-start block the values were computed from."),
                ),
                (
                    "burn",
                    models.FloatField(
                        help_text="Owner-coldkey share of subnet incentive, averaged across mechanisms (0-1).",
                        validators=[
                            django.core.validators.MinValueValidator(0.0),
                            django.core.validators.MaxValueValidator(1.0),
                        ],
                    ),
                ),
                (
                    "superburn",
                    models.FloatField(
                        help_text="Superburn-coldkey share of subnet incentive, averaged across mechanisms (0-1).",
                        validators=[
                            django.core.validators.MinValueValidator(0.0),
                            django.core.validators.MaxValueValidator(1.0),
                        ],
                    ),
                ),
                (
                    "meta_epoch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="burns", to="metagraph.metaepoch"
                    ),
                ),
                (
                    "subnet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="burns", to="metagraph.subnet"
                    ),
                ),
            ],
            options={
                "db_table": "metagraph_subnet_burn",
                "indexes": [models.Index(fields=["meta_epoch", "subnet"], name="idx_subnet_burn_epoch")],
                "constraints": [
                    models.UniqueConstraint(fields=("subnet", "meta_epoch"), name="unique_subnet_burn"),
                    models.CheckConstraint(
                        condition=models.Q(("burn__gte", 0.0), ("burn__lte", 1.0)), name="subnet_burn_between_0_and_1"
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("superburn__gte", 0.0), ("superburn__lte", 1.0)),
                        name="subnet_superburn_between_0_and_1",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="SubnetEmission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("emission_enabled", models.BooleanField()),
                (
                    "meta_epoch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="emissions", to="metagraph.metaepoch"
                    ),
                ),
                (
                    "subnet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE, related_name="emissions", to="metagraph.subnet"
                    ),
                ),
            ],
            options={
                "db_table": "metagraph_subnet_emission",
                "indexes": [models.Index(fields=["meta_epoch", "subnet"], name="idx_subnet_emission_epoch")],
                "constraints": [
                    models.UniqueConstraint(fields=("subnet", "meta_epoch"), name="unique_subnet_emission")
                ],
            },
        ),
        migrations.AddField(
            model_name="metagraphdump",
            name="owner_hotkey",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Subnet owner hotkey at this dump's block. Null when the chain reported no owner, "
                    "or for dumps taken before per-block owners were recorded."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="owned_subnet_dumps",
                to="metagraph.hotkey",
            ),
        ),
        migrations.RunPython(stamp_current_owner_on_existing_dumps, migrations.RunPython.noop),
    ]
