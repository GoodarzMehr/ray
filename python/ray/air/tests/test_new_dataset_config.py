from typing import Optional

import random
import pytest
from unittest.mock import MagicMock

import ray
from ray import train
from ray.train import DataConfig, ScalingConfig
from ray.data import DataIterator
from ray.train.data_parallel_trainer import DataParallelTrainer
from ray.data._internal.execution.interfaces.execution_options import ExecutionOptions
from ray.tests.conftest import *  # noqa


@pytest.fixture
def ray_start_4_cpus():
    address_info = ray.init(num_cpus=4)
    yield address_info
    ray.shutdown()


class TestBasic(DataParallelTrainer):
    def __init__(
        self, num_workers: int, expect_ds: bool, expect_sizes: Optional[dict], **kwargs
    ):
        def train_loop_per_worker():
            data_shard = train.get_dataset_shard("train")
            assert isinstance(data_shard, DataIterator), data_shard
            for k, v in expect_sizes.items():
                shard = train.get_dataset_shard(k)
                if v == -1:
                    assert shard is None, shard
                else:
                    count = 0
                    for batch in shard.iter_batches():
                        for arr in batch.values():
                            count += arr.size
                    assert count == v, shard

        kwargs.pop("scaling_config", None)
        super().__init__(
            train_loop_per_worker=train_loop_per_worker,
            scaling_config=ScalingConfig(num_workers=num_workers),
            **kwargs,
        )


def test_basic(ray_start_4_cpus):
    ds = ray.data.range(10)

    # Single worker basic case.
    test = TestBasic(
        1,
        True,
        {"train": 10, "test": 10},
        datasets={"train": ds, "test": ds},
    )
    test.fit()

    # Single worker, no test ds.
    test = TestBasic(1, True, {"train": 10, "test": -1}, datasets={"train": ds})
    test.fit()

    # Two workers, train and test split.
    test = TestBasic(
        2, True, {"train": 5, "test": 5}, datasets={"train": ds, "test": ds}
    )
    test.fit()

    # Two workers, both split.
    test = TestBasic(
        2,
        True,
        {"train": 5, "test": 5},
        dataset_config=DataConfig(datasets_to_split=["train", "test"]),
        datasets={"train": ds, "test": ds},
    )
    # Test get config.
    assert isinstance(test.get_dataset_config(), DataConfig)
    test.fit()


def test_split(ray_start_4_cpus):
    ds = ray.data.range(10)

    # Split all by default
    test = TestBasic(
        2,
        True,
        {"train": 5, "test": 5, "val": 5},
        datasets={"train": ds, "test": ds, "val": ds},
    )
    test.fit()

    # Test flag "all"
    test = TestBasic(
        2,
        True,
        {"train": 5, "test": 5},
        datasets={"train": ds, "test": ds},
        dataset_config=DataConfig(datasets_to_split="all"),
    )

    # Test split train only.
    test = TestBasic(
        2,
        True,
        {"train": 5, "test": 10},
        datasets={"train": ds, "test": ds},
        dataset_config=DataConfig(datasets_to_split=["train"]),
    )
    test.fit()

    # Test invalid arguments
    for datasets_to_split in ["train", ("train"), {}]:
        with pytest.raises(TypeError, match="`datasets_to_split` should be.*"):
            test = TestBasic(
                2,
                True,
                {"train": 5, "test": 10},
                datasets={"train": ds, "test": ds},
                dataset_config=DataConfig(datasets_to_split=datasets_to_split),
            )

    # Test empty `datasets_to_split` list
    test = TestBasic(
        2,
        True,
        {"train": 10, "test": 10},
        datasets={"train": ds, "test": ds},
        dataset_config=DataConfig(datasets_to_split=[]),
    )
    test.fit()


@pytest.mark.skip(
    reason="Incomplete implementation of _validate_dag causes other errors, so we "
    "remove DAG validation for now; see https://github.com/ray-project/ray/pull/37829"
)
def test_configure_execution_options(ray_start_4_cpus):
    ds = ray.data.range(10)
    # Resource limit is too low and will trigger an error.
    options = DataConfig.default_ingest_options()
    options.resource_limits.cpu = 0
    test = TestBasic(
        1,
        True,
        {"train": 10, "test": 10},
        datasets={"train": ds, "test": ds},
        dataset_config=DataConfig(execution_options=options),
    )
    with pytest.raises(ray.train.base_trainer.TrainingFailedError):
        test.fit()


def test_configure_execution_options_carryover_context(ray_start_4_cpus):
    """Tests that execution options in DataContext are carried over to DatConfig
    automatically."""

    ctx = ray.data.DataContext.get_current()
    ctx.execution_options.preserve_order = True
    ctx.execution_options.verbose_progress = True

    data_config = DataConfig()

    ingest_options = data_config.default_ingest_options()
    assert ingest_options.preserve_order is True
    assert ingest_options.verbose_progress is True


@pytest.mark.parametrize("enable_locality", [True, False])
def test_configure_locality(enable_locality):
    options = DataConfig.default_ingest_options()
    options.locality_with_output = enable_locality
    data_config = DataConfig(execution_options=options)

    mock_ds = MagicMock()
    mock_ds.streaming_split = MagicMock()
    mock_ds.copy = MagicMock(return_value=mock_ds)
    world_size = 2
    worker_handles = [MagicMock() for _ in range(world_size)]
    worker_node_ids = ["node" + str(i) for i in range(world_size)]
    data_config.configure(
        datasets={"train": mock_ds},
        world_size=world_size,
        worker_handles=worker_handles,
        worker_node_ids=worker_node_ids,
    )
    mock_ds.streaming_split.assert_called_once()
    mock_ds.streaming_split.assert_called_with(
        world_size,
        equal=True,
        locality_hints=worker_node_ids if enable_locality else None,
    )


class CustomConfig(DataConfig):
    def __init__(self):
        pass

    def configure(self, *args, **kwargs):
        ds = ray.data.range(10)
        return [
            {"train": ds.iterator()},
            {"train": ds.iterator()},
        ]


def test_custom_config_subclass(ray_start_4_cpus):
    test = TestBasic(
        1,
        True,
        {"train": 10},
        dataset_config=CustomConfig(),
    )
    test.fit()


class TestRandom(DataParallelTrainer):
    def __init__(self, num_workers: int, expect_random: bool, **kwargs):
        def train_loop_per_worker():
            data_shard = train.get_dataset_shard("train")
            assert isinstance(data_shard, DataIterator), data_shard
            epoch1 = list(data_shard.iter_rows())
            epoch2 = list(data_shard.iter_rows())
            print("Epochs", epoch1, "\n", epoch2)
            if expect_random:
                assert epoch1 != epoch2
            else:
                assert epoch1 == epoch2

        kwargs.pop("scaling_config", None)
        super().__init__(
            train_loop_per_worker=train_loop_per_worker,
            scaling_config=ScalingConfig(num_workers=num_workers),
            **kwargs,
        )


def test_per_epoch_preprocessing(ray_start_4_cpus):
    ds = ray.data.range(100, parallelism=100).randomize_block_order()
    test = TestRandom(2, True, datasets={"train": ds})
    test.fit()

    ds = ray.data.range(100, parallelism=100).random_shuffle()
    test = TestRandom(2, True, datasets={"train": ds})
    test.fit()

    ds = ray.data.range(100, parallelism=100).map(
        lambda x: {"id": x["id"] * random.random()}
    )
    test = TestRandom(2, True, datasets={"train": ds})
    test.fit()


def test_materialized_preprocessing(ray_start_4_cpus):
    # TODO(ekl) we should test all these configs with splitting enabled, but this
    # requires implementing deterministic streaming split.
    ds = ray.data.range(100, parallelism=100).randomize_block_order()
    ds = ds.materialize()
    test = TestRandom(
        2,
        False,
        datasets={"train": ds},
        dataset_config=DataConfig(datasets_to_split=[]),
    )
    test.fit()

    ds = ray.data.range(100, parallelism=100).random_shuffle()
    ds = ds.materialize()
    test = TestRandom(
        2,
        False,
        datasets={"train": ds},
        dataset_config=DataConfig(datasets_to_split=[]),
    )
    test.fit()

    ds = ray.data.range(100, parallelism=100).map(
        lambda x: {"id": x["id"] * random.random()}
    )
    ds = ds.materialize()
    test = TestRandom(
        2,
        False,
        datasets={"train": ds},
        dataset_config=DataConfig(datasets_to_split=[]),
    )
    test.fit()


def test_data_config_default_resource_limits(shutdown_only):
    """Test that DataConfig should exclude training resources from Data."""
    cluster_cpus, cluster_gpus = 20, 10
    num_workers = 2
    # Resources used by training workers.
    cpus_per_worker, gpus_per_worker = 2, 1
    # Resources used by the trainer actor.
    default_trainer_cpus, default_trainer_gpus = 1, 0
    num_train_cpus = num_workers * cpus_per_worker + default_trainer_cpus
    num_train_gpus = num_workers * gpus_per_worker + default_trainer_gpus

    init_exclude_cpus = 2
    init_exclude_gpus = 1

    ray.init(num_cpus=cluster_cpus, num_gpus=cluster_gpus)

    class MyTrainer(DataParallelTrainer):
        def __init__(self, **kwargs):
            def train_loop_fn():
                train_ds = train.get_dataset_shard("train")
                exclude_resources = (
                    train_ds._base_dataset.context.execution_options.exclude_resources
                )
                assert exclude_resources.cpu == num_train_cpus + init_exclude_cpus
                assert exclude_resources.gpu == num_train_gpus + init_exclude_gpus

            kwargs.pop("scaling_config", None)

            execution_options = ExecutionOptions()
            execution_options.exclude_resources.cpu = init_exclude_cpus
            execution_options.exclude_resources.gpu = init_exclude_gpus

            super().__init__(
                train_loop_per_worker=train_loop_fn,
                scaling_config=ScalingConfig(
                    num_workers=num_workers,
                    use_gpu=True,
                    resources_per_worker={
                        "CPU": cpus_per_worker,
                        "GPU": gpus_per_worker,
                    },
                ),
                datasets={"train": ray.data.range(10)},
                dataset_config=DataConfig(execution_options=execution_options),
                **kwargs,
            )

    trainer = MyTrainer()
    trainer.fit()


if __name__ == "__main__":
    import sys

    import pytest

    sys.exit(pytest.main(["-v", "-x", __file__]))
