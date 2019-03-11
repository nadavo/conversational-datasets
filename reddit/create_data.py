"""A Dataflow script for creating datasets from reddit.

For usage see README.md.
"""


import argparse
import hashlib
import logging
import os
import re
import uuid
from collections import defaultdict, namedtuple
from functools import partial

import apache_beam as beam
import tensorflow as tf
from apache_beam import pvalue
from apache_beam.io import BigQuerySource, Read
from apache_beam.io.tfrecordio import WriteToTFRecord
from apache_beam.options.pipeline_options import PipelineOptions, SetupOptions


def _parse_args():
    """Parse command line arguments."""

    def _positive_int(value):
        """Define a positive integer ArgumentParser type."""
        value = int(value)
        if value <= 0:
            raise argparse.ArgumentTypeError(
                "Value must be positive, {} was passed.".format(value))
        return value

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reddit_table",
        required=True,
        help="The BigQuery table to read comments from, in "
             "project:table format.",
    )
    parser.add_argument(
        "--output_dir",
        required=True,
        help="Google cloud storage output directory to write the dataset.",
    )
    parser.add_argument(
        "--parent_depth",
        type=_positive_int,
        default=10,
        help="How many parent comments to consider.",
    )
    parser.add_argument(
        "--max_length",
        type=_positive_int,
        default=127,
        help="Maximum length of comments to include.",
    )
    parser.add_argument(
        "--min_length",
        type=_positive_int,
        default=9,
        help="Minimum length of comments to include.",
    )
    parser.add_argument(
        "--train_split",
        default=0.9,
        help="The proportion of data to put in the training set.",
    )
    parser.add_argument(
        "--num_shards_test",
        default=100,
        type=_positive_int,
        help="The number of shards for the test set.",
    )
    parser.add_argument(
        "--num_shards_train",
        default=1000,
        type=_positive_int,
        help="The number of shards for the train set.",
    )
    args, pipeline_args = parser.parse_known_args()


# Represent a reddit comment.
Comment = namedtuple(
    "Comment",
    [
        "id",
        "thread_id",
        "parent_id",
        "body",
        "body_is_trimmed",
        "author",
        "subreddit",
    ]
)


def normalise_comment(comment, max_length):
    """Create a _Comment object from a row in the BigQuery table."""
    return Comment(
        id=comment['id'],
        thread_id=_normalise_id(comment['link_id']),
        parent_id=_normalise_id(comment['parent_id']),
        body=trim(comment['body'], max_length),
        body_is_trimmed=len(comment['body']) > max_length,
        author=comment['author'],
        subreddit=comment['subreddit'],
    )


def _normalise_id(raw_id):
    """Reddit IDs start with t1_, t2_, etc. which need to be stripped."""
    return re.sub("^t[0-9]_", "", raw_id)


def trim(text, max_length):
    """Trims text to be at most `max_length`, without splitting apart words."""
    if len(text) <= max_length:
        return text

    text = text[:max_length + 1]

    # Trim until the last two characters are the boundary between an
    # alphanumeric character, and a non-alphanumeric character.
    while text and (text[-1].isalnum() == text[-2].isalnum()):
        text = text[:-1]

    return text[:-1]


def _should_skip(comment, min_length):
    if comment.body_is_trimmed:
        return True
    if comment.body in {"[deleted]", "[removed]"}:
        return True
    if len(comment.body) < min_length:
        return True
    return False


def create_examples(thread, parent_depth, min_length):
    """Creates serialized tensorflow examples from a reddit thread."""
    id_to_comment = {comment.id: comment for comment in list(thread)}

    for linear_path in linear_paths(id_to_comment, parent_depth):
        response = id_to_comment[linear_path[-1]]
        context = id_to_comment[linear_path[-2]]  # guaranteed to exist.

        if (_should_skip(response, min_length)
                or _should_skip(context, min_length)):
            continue

        example = tf.train.Example()

        _add_string_feature(example, "subreddit", response.subreddit)
        _add_string_feature(example, "thread_id", response.thread_id)
        _add_string_feature(example, "context_author", context.author)
        _add_string_feature(example, "response_author", response.author)
        _add_string_feature(example, "context", context.body)
        _add_string_feature(example, "response", response.body)
        for i in range(parent_depth - 1):
            # Extra contexts start at index -3.
            index = -3 - i
            try:
                context_i = linear_path[index]
            except IndexError:
                break

            _add_string_feature(
                example, "context/{}".format(i),
                id_to_comment[context_i].body)

        yield example.SerializeToString()


def linear_paths(id_to_comment, parent_depth):
    """Gets all linear paths of comments and replies from the thread.

    Each linear path is guaranteed to have at least two comments in it.
    """
    paths = []
    seen_ids = set()
    id_to_children = defaultdict(list)
    for comment_id, comment in id_to_comment.items():
        id_to_children[comment.parent_id].append(comment_id)
        if comment.parent_id not in id_to_comment:
            paths.append([comment_id])
            seen_ids.add(comment_id)

    while paths:
        new_paths = []
        for path in paths:
            last_id = path[-1]
            for child_id in id_to_children[last_id]:
                if child_id in seen_ids:
                    # Prevent infinite loops.
                    continue
                seen_ids.add(child_id)
                new_path = path[-parent_depth:] + [child_id]
                new_paths.append(new_path)
                yield new_path
        paths = new_paths


def _add_string_feature(example, feature_name, value):
    """Adds a string feature to a tensorflow example."""
    example.features.feature[feature_name].bytes_list.value.append(
        value.encode("utf-8"))


def _shuffle(pcollection):
    """Shuffles the input pcollection."""
    pcollection |= "add random key" >> beam.Map(
        lambda value: (uuid.uuid4(), value))
    pcollection |= "group by key" >> beam.GroupByKey()
    pcollection |= "get shuffled values" >> beam.FlatMap(lambda t: t[1])
    return pcollection


class _TrainTestSplitFn(beam.DoFn):
    """Splits an input PCollection of serialized examples into train and test.

    This uses the thread id to compute the split, so that examples from the
    same thread are in the same set. The split is deterministic based on
    thread id, so that multiple runs produce the same result.
    """

    TRAIN_TAG = "train"
    TEST_TAG = "test"

    def __init__(self, train_split, num_buckets=4096):
        super(_TrainTestSplitFn, self).__init__()
        self._train_split = train_split
        self._num_buckets = num_buckets

    def process(self, serialized_example):
        example = tf.train.Example()
        example.ParseFromString(serialized_example)

        thread_id, = example.features.feature['thread_id'].bytes_list.value
        split_value = self._split_value(thread_id)

        split = (
            self.TRAIN_TAG if split_value < self._train_split else
            self.TEST_TAG)
        yield pvalue.TaggedOutput(split, serialized_example)

    def _split_value(self, thread_id):
        """Compute a value from 0 to 1 used to compute the split."""
        md5 = hashlib.md5()
        md5.update(thread_id)
        md5_digest = int(md5.hexdigest(), 16)
        return (
            (1 + md5_digest % self._num_buckets)
            / float(self._num_buckets)
        )


def _main():
    args, pipeline_args = _parse_args()

    pipeline_options = PipelineOptions(pipeline_args)
    pipeline_options.view_as(SetupOptions).save_main_session = True
    p = beam.Pipeline(options=pipeline_options)
    comments = p | ("Read " + args.reddit_table) >> Read(
        BigQuerySource(args.reddit_table))
    comments |= (
        "Normalise comments" >> beam.Map(
            partial(normalise_comment, max_length=args.max_length)))

    thread_id_to_comments = comments | (
        "Key by thread id" >> beam.Map(
            lambda comment: (comment.thread_id, comment)))
    threads = thread_id_to_comments | (
        "Group comments by thread ID" >> beam.GroupByKey())
    threads = threads | ("Get threads" >> beam.Map(lambda t: t[1]))

    serialized_examples = threads | (
        "Create TF examples" >> beam.FlatMap(
            partial(create_examples,
                    parent_depth=args.parent_depth,
                    min_length=args.min_length)))
    serialized_examples = _shuffle(serialized_examples)

    serialized_examples |= "split train and test" >> beam.ParDo(
        _TrainTestSplitFn(args.train_split)
    ).with_outputs(_TrainTestSplitFn.TEST_TAG, _TrainTestSplitFn.TRAIN_TAG)

    (
        serialized_examples[_TrainTestSplitFn.TRAIN_TAG]
        | "write train"
        >> WriteToTFRecord(
            os.path.join(args.output_dir, "train"),
            file_name_suffix=".tfrecords",
            num_shards=args.num_shards_train,
        )
    )
    (
        serialized_examples[_TrainTestSplitFn.TEST_TAG]
        | "write test"
        >> WriteToTFRecord(
            os.path.join(args.output_dir, "test"),
            file_name_suffix=".tfrecords",
            num_shards=args.num_shards_test,
        )
    )

    result = p.run()
    result.wait_until_finish()


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    _main()
