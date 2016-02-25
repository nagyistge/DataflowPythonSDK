# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A streaming word-counting workflow.

Important: streaming pipeline support in Python Dataflow is in development
and is not yet available for use.
"""

from __future__ import absolute_import

import logging
import re


import google.cloud.dataflow as df
from google.cloud.dataflow.transforms.trigger import AccumulationMode
from google.cloud.dataflow.transforms.trigger import AfterCount
import google.cloud.dataflow.transforms.window as window
from google.cloud.dataflow.utils.options import add_option
from google.cloud.dataflow.utils.options import get_options


def run(options=None):
  """Build and run the pipeline."""

  p = df.Pipeline(options=get_options(options))

  # Read the text file[pattern] into a PCollection.
  lines = p | df.io.Read(
      'read', df.io.PubSubSource(p.options.input_topic))

  # Capitalize the characters in each line.
  transformed = (lines
                 | (df.FlatMap('split', lambda x: re.findall(r'[A-Za-z\']+', x))
                    .with_output_types(unicode))
                 | df.Map('pair_with_one', lambda x: (x, 1))
                 | df.WindowInto(window.FixedWindows(60, 0),
                                 trigger=AfterCount(3),
                                 accumulation_mode=AccumulationMode.DISCARDING)
                 | df.GroupByKey('group')
                 | df.Map('count', lambda (word, ones): (word, sum(ones)))
                 | df.Map('format', lambda tup: '%s: %d' % tup))

  # Write to PubSub.
  # pylint: disable=expression-not-assigned
  transformed | df.io.Write(
      'pubsub_write', df.io.PubSubSink(p.options.output_topic))

  p.run()

add_option(
    '--input_topic', dest='input_topic', required=True,
    help='Input PubSub topic of the form "/topics/<PROJECT>/<TOPIC>".')
add_option(
    '--output_topic', dest='output_topic', required=True,
    help='Output PubSub topic of the form "/topics/<PROJECT>/<TOPIC>".')


if __name__ == '__main__':
  logging.getLogger().setLevel(logging.INFO)
  run()
