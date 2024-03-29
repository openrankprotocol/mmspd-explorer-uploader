import argparse
import asyncio
import contextlib
import json
import pathlib
import tempfile
import traceback
import zipfile
from argparse import ArgumentParser
from datetime import datetime
from functools import reduce

import aioboto3
import structlog

_logger: structlog.BoundLoggerBase = structlog.get_logger(__name__)


async def setup_parser(parser: ArgumentParser):
    parser.add_argument('--aws-profile',
                        help="""AWS CLI profile name""")
    parser.add_argument('--aws-region',
                        help="""AWS region name""")
    parser.add_argument('--s3-uploaders', metavar='NUM', type=int, default=10,
                        help="""number of parallel S3 uploader tasks""")
    parser.add_argument('s3_bucket', metavar='BUCKET',
                        help="""AWS S3 bucket name""")
    parser.add_argument('indexer_cache', type=pathlib.Path,
                        help="""cache CSV file for /indexer-scores""")
    parser.add_argument('directories', metavar='DIRECTORY', type=pathlib.Path,
                        nargs='+',
                        help="""output directory to scan""")


def parse_timestamp(s: str) -> datetime:
    try:
        return datetime.strptime(s, '%Y-%m-%dT%H:%M:%S.%f%z')
    except ValueError:
        return datetime.strptime(s, '%Y-%m-%dT%H:%M:%S%z')


def rm_rf(path: pathlib.Path):
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            rm_rf(child)
        path.rmdir()
    else:
        path.unlink(missing_ok=True)


async def upload_to_s3(args: argparse.Namespace, worker_index: int,
                       queue: asyncio.Queue):
    logger = _logger
    session = aioboto3.Session(profile_name=args.aws_profile,
                               region_name=args.aws_region)
    my_name = f'uploader-{worker_index}'
    logger = logger.bind(worker=my_name)
    bucket = None
    async with contextlib.AsyncExitStack() as stack:
        while True:
            match await queue.get():
                case None:
                    await queue.put(None)
                    # logger.debug("finished")
                    return
                case (path, key):
                    # noinspection PyBroadException
                    try:
                        if bucket is None:
                            s3 = await stack.enter_async_context(
                                session.resource('s3'))
                            bucket = await s3.Bucket(args.s3_bucket)
                        await bucket.upload_file(f'{path}', key)
                        logger.info("uploaded", path=path, key=key,
                                    bucket=args.s3_bucket)
                    except Exception:
                        logger.error("upload failed", path=path, key=key,
                                     bucket=args.s3_bucket,
                                     exception=traceback.format_exc())


async def run(args):
    # TODO(ek): Use some sort of filesystem monitor
    manifest_by_scope_ts = dict[str, dict[int, dict]]()
    timestamps_by_epoch_scope = dict[datetime, dict[str, set[int]]]()
    while True:
        _logger.info("starting a run")
        s3_upload_queue = asyncio.Queue(args.s3_uploaders * 4)
        uploaders = [
            asyncio.create_task(
                upload_to_s3(args, i, s3_upload_queue))
            for i in range(args.s3_uploaders)
        ]
        tmpdir = pathlib.Path(tempfile.mkdtemp())
        try:
            for directory in args.directories:
                directory: pathlib.Path
                for path in directory.iterdir():
                    logger = _logger.bind(path=path)
                    match path.suffix:
                        case '.json':
                            try:
                                ts0 = int(path.stem)
                            except ValueError:
                                continue
                            try:
                                with path.open() as f:
                                    manifest = json.load(f)
                            except (OSError, json.JSONDecodeError):
                                logger.error("cannot load manifest",
                                             exc=traceback.format_exc())
                                continue
                            try:
                                epoch = parse_timestamp(manifest['epoch'])
                                ts = parse_timestamp(manifest['issuanceDate'])
                                scope = manifest['scope']
                            except KeyError:
                                # logger.error("invalid manifest",
                                #              exc=traceback.format_exc())
                                continue
                            except ValueError:
                                logger.error(
                                    "invalid epoch/issuance/effective date",
                                    exc=traceback.format_exc())
                                continue
                            if not isinstance(scope, str):
                                logger.error("scope not a string")
                                continue
                            assert round(ts.timestamp() * 1000) == ts0
                            try:
                                manifest_by_scope_ts[scope][ts0]
                            except KeyError:
                                pass
                            else:
                                continue
                            with zipfile.ZipFile(
                                    path.with_suffix('.zip')) as z:
                                zipdir = tmpdir / scope / path.stem
                                z.extractall(zipdir)
                                for path2 in zipdir.iterdir():
                                    if path2.is_file():
                                        relpath = path2.relative_to(zipdir)
                                        await s3_upload_queue.put((
                                            f'{path2}',
                                            f'files/{scope}/{ts0}/{relpath}',
                                        ))
                                        # for backward compatibility
                                        await s3_upload_queue.put((
                                            f'{path2}',
                                            f'files/{ts0}/{relpath}',
                                        ))
                            manifest_by_scope_ts \
                                .setdefault(scope, {})[ts0] \
                                = manifest
                            timestamps_by_epoch_scope \
                                .setdefault(epoch, {}) \
                                .setdefault(scope, set()) \
                                .add(ts0)
            try:
                max_epoch = max(timestamps_by_epoch_scope.keys())
            except ValueError:
                continue
            timestamps = [str(ts)
                          for ts in sorted(reduce(lambda x, y: x | y,
                                                  timestamps_by_epoch_scope[
                                                      max_epoch].values()))]
            timestamps_json = tmpdir / 'timestamps.json'
            with timestamps_json.open('w') as f:
                json.dump(timestamps, f)
            await s3_upload_queue.put(
                (f'{timestamps_json}', 'api/scores/timestamps.json'))
            list_ = {}
            for (scope, timestamps) in \
                    timestamps_by_epoch_scope[max_epoch].items():
                for ts in timestamps:
                    manifest = manifest_by_scope_ts[scope][ts]
                    list_[str(ts)] = manifest
            list_json = tmpdir / 'list.json'
            with list_json.open('w') as f:
                json.dump(list_, f)
            await s3_upload_queue.put(
                (f'{list_json}', 'api/scores/list.json'))
            await s3_upload_queue.put(
                (f'{args.indexer_cache}', 'api/scores/indexer-scores'))
            _logger.info("finished a run")
        finally:
            await s3_upload_queue.put(None)
            await asyncio.gather(*uploaders, return_exceptions=True)
            _logger.info("all uploads finished")
            rm_rf(tmpdir)
        await asyncio.sleep(10)
