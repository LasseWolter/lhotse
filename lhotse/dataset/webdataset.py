import pickle
from typing import Callable, Dict, Optional, Sequence, Union

from tqdm.auto import tqdm

from lhotse import CutSet
from lhotse.serialization import LazyIteratorChain
from lhotse.utils import Pathlike, is_module_available


def export_to_webdataset(
    cuts: CutSet,
    output_path: Pathlike,
    shard_size: Optional[int] = None,
    verbose: bool = True,
    audio_format: str = "flac",
    load_audio: bool = True,
    load_features: bool = True,
    load_custom: bool = True,
) -> int:
    """
    Saves the CutSet metadata along with audio/features data into a WebDataset archive.
    The audio and feature data is read, decoded, and encoded into ``audio_format`` for audio,
    lilcom for features and arrays with floating point type, and pickle for all other dtypes.
    The intended use of this function is to speed up the I/O in training data pipelines by
    converting random access reads to sequential access reads.

    Supported values for ``audio_format`` are the same as for the ``format`` argument in
    ``torchaudio.save`` function with ``sox_io`` backend.

    If ``shard_size`` is specified, we will leverage WebDataset's ``ShardWriter`` to
    create multiple tarballs with ``shard_size`` items per shard. In that mode, we expect
    that ``output_path`` contains a pattern like "/path/to/shard-%06d.tar", which will
    be internally expanded with the shard index.

    Returns number of written shards if sharding is enabled, otherwise 0.

    **Examples**

    Export cuts with audio, features, and all custom data to a single tarball,
    converting audio to FLACs::

        >>> cuts = CutSet.from_jsonl_lazy("data/cuts-train.jsonl")
        >>> n_shards = export_to_webdataset(
        ...     cuts=cuts,
        ...     output_path="data/cuts-train.tar",
        ...     audio_format="flac",
        ... )

    Export cuts with audio, features, and all custom data to a directory with shards
    counting 10000 cuts each, converting audio to SPHERE (sph)::

        >>> cuts = CutSet.from_jsonl_lazy("data/cuts-train.jsonl")
        >>> n_shards = export_to_webdataset(
        ...     cuts=cuts,
        ...     output_path="data/cuts-train-wds/shard-%06d.tar",
        ...     shard_size=10000,
        ...     audio_format="sph",
        ... )

    The same, but export cuts with only the features being read into memory
    (recording and custom data still refers to external storage)::

        >>> cuts = CutSet.from_jsonl_lazy("data/cuts-train.jsonl")
        >>> n_shards = export_to_webdataset(
        ...     cuts=cuts,
        ...     output_path="data/cuts-train-wds/shard-%06d.tar",
        ...     shard_size=10000,
        ...     load_audio=False,
        ...     load_custom=False,
        ... )

    Export cuts to sharded tarballs stored in the cloud
    (in this example AWS S3, using AWS CLI)::

        >>> cuts = CutSet.from_jsonl_lazy("data/cuts-train.jsonl")
        >>> n_shards = export_to_webdataset(
        ...     cuts=cuts,
        ...     output_path="pipe:aws s3 cp - s3://my-bucket/data/shard-%06d.tar",
        ...     shard_size=10000,
        ... )
    """
    if not is_module_available("webdataset"):
        raise ImportError("Please 'pip install webdataset' first.")
    from webdataset import TarWriter

    if shard_size is not None:
        assert shard_size > 0
        # Note: this ShardWriter is not from webdataset, but defined below in this file.
        sink = ShardWriter(output_path, maxcount=shard_size)
    else:
        sink = TarWriter(output_path)

    num_shards_written = 0
    with sink:
        for idx, cut in tqdm(
            enumerate(cuts), desc="Creating WebDataset tarball(s)", disable=not verbose
        ):
            cut = cut.move_to_memory(
                audio_format=audio_format,
                load_audio=load_audio,
                load_features=load_features,
                load_custom=load_custom,
            )
            data = pickle.dumps(cut.to_dict())
            sink.write({"__key__": cut.id, "data": data})

        if isinstance(sink, ShardWriter):
            num_shards_written = sink.shard

    return num_shards_written


class LazyWebdatasetIterator:
    """
    LazyWebdatasetIterator provides the ability to read Lhotse objects from a
    WebDataset tarball on-the-fly, without reading its full contents into memory.

    This class is designed to be a partial "drop-in" replacement for ordinary dicts
    to support lazy loading of RecordingSet, SupervisionSet and CutSet.
    Since it does not support random access reads, some methods of these classes
    might not work properly.

    The behaviour of the underlying ``WebDataset`` instance can be customized by
    providing its kwargs directly to the constructor of this class.
    """

    def __init__(
        self, source: Union[Pathlike, Sequence[Pathlike]], **wds_kwargs
    ) -> None:
        if not is_module_available("webdataset"):
            raise ImportError("Please 'pip install webdataset' first.")

        self.source = source
        self.wds_kwargs = wds_kwargs

    def set_epoch(self, epoch: int) -> None:
        self.wds_kwargs["epoch"] = epoch

    def _reset(self) -> None:
        if not is_module_available("webdataset"):
            raise ImportError("Please 'pip install webdataset' first.")

        self._ds = mini_webdataset(self.source, **self.wds_kwargs)
        self._ds_iter = iter(self._ds)

    def __getstate__(self) -> dict:
        """
        Store the state for pickling -- we'll only store the path + kwargs, and re-initialize
        this iterator when unpickled. This is necessary to transfer this object across processes
        for PyTorch's DataLoader workers.
        """
        state = {"source": self.source, "wds_kwargs": self.wds_kwargs}
        return state

    def __setstate__(self, state: Dict) -> None:
        """Restore the state when unpickled."""
        self.__dict__.update(state)

    def __iter__(self) -> "LazyWebdatasetIterator":
        self._reset()
        return self

    def __next__(self):
        from lhotse.serialization import deserialize_item

        data_dict = next(self._ds_iter)
        data = pickle.loads(data_dict["data"])
        item = deserialize_item(data)
        return item

    def values(self):
        yield from self

    def keys(self) -> str:
        return (item.id for item in self)

    def items(self):
        return ((item.id, item) for item in self)

    def __add__(self, other) -> LazyIteratorChain:
        return LazyIteratorChain(self, other)


def mini_webdataset(
    urls: Union[Pathlike, Sequence[Pathlike]],
    epoch: int = 0,
    repeat: bool = False,
    shuffle_shards: bool = False,
    shuffle: bool = False,
    split_by_worker: bool = False,
    split_by_node: bool = False,
    shuffle_bufsize: int = 1000,
    ignore_error_shards: bool = True,
):
    """
    Return a pipeline for WebDataset-style data files.

    This is a convenience function for constructing a partial pipeline
    that reads from a set of sharded tar files, extracts the individual
    files, and groups them together into samples (dictionaries).

    You can use all the methods from `Composable` (`then`, `compose`) and
    from `Shorthands` (`batched`, `unbatched`, `decode`, `shuffle`, etc.)
    on the result.

    .. note: This is a reduced version of ``webdataset.WebDataset`` function,
        that only uses the functionalities relevant to Lhotse, and makes it
        possible to disable the node/worker splitting.

    :param urls: the source URLs: a string or a list.
    :param epoch: epoch number (used only when shuffling is enabled).
    :param repeat: repeat infinitely if True.
    :param shuffle: shuffle the items if True (after shuffling the shards, if enabled).
        Note: ``shuffle`` is seeded with PID and time, making it non-reproducible across processes.
    :param shuffle_shards: shuffle the shards if True.
        Only takes effect when ``urls`` is a list of shard paths/urls.
    :param split_by_worker: if True, shards are split per DataLoader worker subprocesses,
        otherwise each dataloader worker will yield the same data.
        Only takes effect when ``urls`` is a list of shard paths/urls.
    :param split_by_node: if True, shards are split per node in DDP training,
        otherwise on each node we'll yield the same data.
        Only takes effect when ``urls`` is a list of shard paths/urls.
    :param shuffle_bufsize: Buffer size for the ``shuffle`` argument.
        Larger bufsize means more memory usage but potentially improved randomness.
    :param ignore_error_shards: when ``True``, we tell WebDataset to ignore shards that
        failed during loading and emit a warning. When ``False``, we won't catch the exceptions.
    """
    if not is_module_available("webdataset"):
        raise ImportError("Please 'pip install webdataset' first.")

    from webdataset import PytorchShardList, reraise_exception, warn_and_continue
    from webdataset import tariterators

    handler = warn_and_continue if ignore_error_shards else reraise_exception

    result = PytorchShardList(
        urls,
        shuffle=shuffle_shards,
        split_by_worker=split_by_worker,
        split_by_node=split_by_node,
    )
    result.set_epoch(epoch)
    result = result.then(tariterators.url_opener, handler=handler)
    result = result.then(tariterators.tar_file_expander, handler=handler)
    result = result.then(tariterators.group_by_keys, handler=handler)
    if repeat:
        result = result.repeat()
    if shuffle:
        result = result.shuffle(shuffle_bufsize)
    return result


class ShardWriter:
    """
    Like ``webdataset.TarWriter`` but splits into multiple shards.

    Note: this implementation is copied from webdataset and adapted to
    allow shard writing using the "pipe:" notation. E.g., this is possible::

        >>> writer = ShardWriter("pipe:gzip -c > data/shard-%06d.tar.gz")

    Source:
    https://github.com/webdataset/webdataset/blob/ccfe88086cdb21a0dc23a6454ce3e3723b6b8033/webdataset/writer.py#L359
    """

    def __init__(
        self,
        pattern: str,
        maxcount: int = 100000,
        maxsize: float = 3e9,
        post: Optional[Callable] = None,
        start_shard: int = 0,
        **kw,
    ):
        """Create a ShardWriter.

        :param pattern: output file pattern
        :param maxcount: maximum number of records per shard (Default value = 100000)
        :param maxsize: maximum size of each shard (Default value = 3e9)
        :param kw: other options passed to TarWriter
        """
        if not is_module_available("webdataset"):
            raise ImportError("Please 'pip install webdataset' first.")

        self.verbose = 1
        self.kw = kw
        self.maxcount = maxcount
        self.maxsize = maxsize
        self.post = post

        self.tarstream = None
        self.shard = start_shard
        self.pattern = pattern
        self.total = 0
        self.count = 0
        self.size = 0
        self.fname = None
        self.next_stream()

    def next_stream(self):
        """Close the current stream and move to the next."""
        from webdataset.writer import TarWriter

        self.finish()
        self.fname = self.pattern % self.shard
        if self.verbose:
            print(
                "# writing",
                self.fname,
                self.count,
                "%.1f GB" % (self.size / 1e9),
                self.total,
            )
        self.shard += 1
        self.tarstream = TarWriter(self.fname, **self.kw)
        self.count = 0
        self.size = 0

    def write(self, obj):
        """Write a sample.

        :param obj: sample to be written
        """
        if (
            self.tarstream is None
            or self.count >= self.maxcount
            or self.size >= self.maxsize
        ):
            self.next_stream()
        size = self.tarstream.write(obj)
        self.count += 1
        self.total += 1
        self.size += size

    def finish(self):
        """Finish all writing (use close instead)."""
        if self.tarstream is not None:
            self.tarstream.close()
            assert self.fname is not None
            if callable(self.post):
                self.post(self.fname)
            self.tarstream = None

    def close(self):
        """Close the stream."""
        self.finish()
        del self.tarstream
        del self.shard
        del self.count
        del self.size

    def __enter__(self):
        """Enter context."""
        return self

    def __exit__(self, *args, **kw):
        """Exit context."""
        self.close()
