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

"""Support for Dataflow triggers.

Triggers control when in processing time windows get emitted.
"""

from abc import ABCMeta
from abc import abstractmethod
import collections
import copy

from google.cloud.dataflow.transforms import combiners
from google.cloud.dataflow.transforms import core
from google.cloud.dataflow.transforms.window import GlobalWindow
from google.cloud.dataflow.transforms.window import WindowFn


class AccumulationMode(object):
  """Controls what to do with data when a trigger fires multiple times.
  """
  DISCARDING = 1
  ACCUMULATING = 2
  # TODO(robertwb): Provide retractions of previous outputs.
  # RETRACTING = 3


class StateTag(object):
  """An identifier used to store and retrieve typed, combinable state.

  The given tag must be unique for this stage.  If CombineFn is None then
  all elements will be returned as a list, otherwise the given CombineFn
  will be applied (possibly incrementally and eagerly) when adding elements.
  """
  __metaclass__ = ABCMeta

  def __init__(self, tag):
    self.tag = tag


class ValueStateTag(StateTag):
  """StateTag pointing to an element."""

  def __repr__(self):
    return 'ValueStateTag(%s, %s)' % (self.tag, self.combine_fn)

  def with_prefix(self, prefix):
    return ValueStateTag(prefix + self.tag)


class CombiningValueStateTag(StateTag):
  """StateTag pointing to an element, accumulated with a combiner."""

  # TODO(robertwb): Also store the coder (perhaps extracted from the combine_fn)
  def __init__(self, tag, combine_fn):
    super(CombiningValueStateTag, self).__init__(tag)
    if not combine_fn:
      raise ValueError('combine_fn must be specified.')
    if not isinstance(combine_fn, core.CombineFn):
      combine_fn = core.CombineFn.from_callable(combine_fn)
    self.combine_fn = combine_fn

  def __repr__(self):
    return 'CombiningValueStateTag(%s, %s)' % (self.tag, self.combine_fn)

  def with_prefix(self, prefix):
    return CombiningValueStateTag(prefix + self.tag, self.combine_fn)


class ListStateTag(StateTag):
  """StateTag pointing to a list of elements."""

  def __init__(self, tag):
    super(ListStateTag, self).__init__(tag)

  def __repr__(self):
    return 'ListStateTag(%s)' % self.tag

  def with_prefix(self, prefix):
    return ListStateTag(prefix + self.tag)


# pylint: disable=unused-argument
# TODO(robertwb): Provisional API, Java likely to change as well.
class TriggerFn(object):
  """A TriggerFn determines when window (panes) are emitted.

  See https://cloud.google.com/dataflow/model/triggers.
  """
  __metaclass__ = ABCMeta

  @abstractmethod
  def on_element(self, element, window, context):
    """Called when a new element arrives in a window.

    Args:
      element: the element being added
      window: the window to which the element is being added
      context: a context (e.g. a TriggerContext instance) for managing state
          and setting timers
    """
    pass

  @abstractmethod
  def on_merge(self, to_be_merged, merge_result, context):
    """Called when multiple windows are merged.

    Args:
      to_be_merged: the set of windows to be merged
      merge_result: the window into which the windows are being merged
      context: a context (e.g. a TriggerContext instance) for managing state
          and setting timers
    """
    pass

  @abstractmethod
  def should_fire(self, watermark, window, context):
    """Whether this trigger should cause the window to fire.

    Args:
      watermark: (a lower bound on) the watermark of the system
      window: the window whose trigger is being considered
      context: a context (e.g. a TriggerContext instance) for managing state
          and setting timers

    Returns:
      whether this trigger should cause a firing
    """
    pass

  @abstractmethod
  def on_fire(self, watermark, window, context):
    """Called when a trigger actually fires.

    Args:
      watermark: (a lower bound on) the watermark of the system
      window: the window whose trigger is being fired
      context: a context (e.g. a TriggerContext instance) for managing state
          and setting timers

    Returns:
      whether this trigger is finished
    """
    pass

  @abstractmethod
  def reset(self, window, context):
    """Clear any state and timers used by this TriggerFn."""
    pass
# pylint: enable=unused-argument


class DefaultTrigger(TriggerFn):
  """Semantically Repeatedly(AfterWatermark()), but more optimized."""

  def __init__(self):
    pass

  def __repr__(self):
    return 'DefaultTrigger()'

  def on_element(self, element, window, context):
    context.set_timer('', window.end)

  def on_merge(self, to_be_merged, merge_result, context):
    # Note: Timer clearing solely an optimization.
    for window in to_be_merged:
      if window.end != merge_result.end:
        context.clear_timer('')

  def should_fire(self, watermark, window, context):
    return watermark >= window.end

  def on_fire(self, watermark, window, context):
    return False

  def reset(self, window, context):
    context.clear_timer('')

  def __eq__(self, other):
    return type(self) == type(other)


class AfterWatermark(TriggerFn):
  """Fire exactly once when the watermark passes the end of the window.

  Args:
      early: if not None, a speculative trigger to repeatedly evaluate before
        the watermark passes the end of the window
      late: if not None, a speculative trigger to repeatedly evaluate after
        the watermark passes the end of the window
  """
  LATE_TAG = CombiningValueStateTag('is_late', any)

  def __init__(self, early=None, late=None):
    self.early = Repeatedly(early) if early else None
    self.late = Repeatedly(late) if late else None

  def __repr__(self):
    qualifiers = []
    if self.early:
      qualifiers.append('early=%s' % self.early)
    if self.late:
      qualifiers.append('late=%s', self.late)
    return 'AfterWatermark(%s)' % ', '.join(qualifiers)

  def is_late(self, context):
    return self.late and context.get_state(self.LATE_TAG)

  def on_element(self, element, window, context):
    if self.is_late(context):
      self.late.on_element(element, window, NestedContext(context, 'late'))
    else:
      context.set_timer('', window.end)
      if self.early:
        self.early.on_element(element, window, NestedContext(context, 'early'))

  def on_merge(self, to_be_merged, merge_result, context):
    # TODO(robertwb): Figure out whether the 'rewind' semantics could be used
    # here.
    if self.is_late(context):
      self.late.on_merge(
          to_be_merged, merge_result, NestedContext(context, 'late'))
    else:
      # Note: Timer clearing solely an optimization.
      for window in to_be_merged:
        if window.end != merge_result.end:
          context.clear_timer('')
      if self.early:
        self.early.on_merge(
            to_be_merged, merge_result, NestedContext(context, 'early'))

  def should_fire(self, watermark, window, context):
    if self.is_late(context):
      return self.late.should_fire(
          watermark, window, NestedContext(context, 'late'))
    elif watermark >= window.end:
      return True
    elif self.early:
      return self.early.should_fire(
          watermark, window, NestedContext(context, 'early'))
    else:
      return False

  def on_fire(self, watermark, window, context):
    if self.is_late(context):
      return self.late.on_fire(
          watermark, window, NestedContext(context, 'late'))
    elif watermark >= window.end:
      context.add_state(self.LATE_TAG, True)
      return not self.late
    elif self.early:
      self.early.on_fire(watermark, window, NestedContext(context, 'early'))
      return False

  def reset(self, window, context):
    if self.late:
      context.clear_state(self.LATE_TAG)
    if self.early:
      self.early.reset(window, NestedContext(context, 'early'))
    if self.late:
      self.late.reset(window, NestedContext(context, 'late'))

  def __eq__(self, other):
    return (type(self) == type(other)
            and self.early == other.early
            and self.late == other.late)

  def __hash__(self):
    return hash((type(self), self.early, self.late))


class AfterCount(TriggerFn):
  """Fire when there are at least count elements in this window pane."""

  COUNT_TAG = CombiningValueStateTag('count', combiners.CountCombineFn())

  def __init__(self, count):
    self.count = count

  def __repr__(self):
    return 'AfterCount(%s)' % self.count

  def on_element(self, element, window, context):
    context.add_state(self.COUNT_TAG, 1)

  def on_merge(self, to_be_merged, merge_result, context):
    # states automatically merged
    pass

  def should_fire(self, watermark, window, context):
    return context.get_state(self.COUNT_TAG) >= self.count

  def on_fire(self, watermark, window, context):
    return True

  def reset(self, window, context):
    context.clear_state(self.COUNT_TAG)


class Repeatedly(TriggerFn):
  """Repeatedly invoke the given trigger, never finishing."""

  def __init__(self, underlying):
    self.underlying = underlying

  def __repr__(self):
    return 'Repeatedly(%s)' % self.underlying

  def on_element(self, element, window, context):  # get window from context?
    self.underlying.on_element(element, window, context)

  def on_merge(self, to_be_merged, merge_result, context):
    self.underlying.on_merge(to_be_merged, merge_result, context)

  def should_fire(self, watermark, window, context):
    return self.underlying.should_fire(watermark, window, context)

  def on_fire(self, watermark, window, context):
    if self.underlying.on_fire(watermark, window, context):
      self.underlying.reset(window, context)
    return False

  def reset(self, window, context):
    self.underlying.reset(window, context)


class ParallelTriggerFn(TriggerFn):

  __metaclass__ = ABCMeta

  def __init__(self, *triggers):
    self.triggers = triggers

  def __repr__(self):
    return '%s(%s)' % (self.__class__.__name__,
                       ', '.join(str(t) for t in self.triggers))

  @abstractmethod
  def combine_op(self, trigger_results):
    pass

  def on_element(self, element, window, context):
    for ix, trigger in enumerate(self.triggers):
      trigger.on_element(element, window, self._sub_context(context, ix))

  def on_merge(self, to_be_merged, merge_result, context):
    for ix, trigger in enumerate(self.triggers):
      trigger.on_merge(
          to_be_merged, merge_result, self._sub_context(context, ix))

  def should_fire(self, watermark, window, context):
    return self.combine_op(
        trigger.should_fire(watermark, window, self._sub_context(context, ix))
        for ix, trigger in enumerate(self.triggers))

  def on_fire(self, watermark, window, context):
    finished = []
    for ix, trigger in enumerate(self.triggers):
      nested_context = self._sub_context(context, ix)
      if trigger.should_fire(watermark, window, nested_context):
        finished.append(trigger.on_fire(watermark, window, nested_context))
    return self.combine_op(finished)

  def reset(self, window, context):
    for ix, trigger in enumerate(self.triggers):
      trigger.reset(window, self._sub_context(context, ix))

  @staticmethod
  def _sub_context(context, index):
    return NestedContext(context, '%d/' % index)


class AfterFirst(ParallelTriggerFn):
  """Fires when any subtrigger fires.

  Also finishes when any subtrigger finishes.
  """
  combine_op = any


class AfterAll(ParallelTriggerFn):
  """Fires when all subtriggers have fired.

  Also finishes when all subtriggers have finished.
  """
  combine_op = all


class AfterEach(TriggerFn):

  INDEX_TAG = CombiningValueStateTag('index', (
      lambda indices: 0 if not indices else max(indices)))

  def __init__(self, *triggers):
    self.triggers = triggers

  def __repr__(self):
    return '%s(%s)' % (self.__class__.__name__,
                       ', '.join(str(t) for t in self.triggers))

  def on_element(self, element, window, context):
    ix = context.get_state(self.INDEX_TAG)
    if ix < len(self.triggers):
      self.triggers[ix].on_element(
          element, window, self._sub_context(context, ix))

  def on_merge(self, to_be_merged, merge_result, context):
    # This takes the furthest window on merging.
    # TODO(robertwb): Revisit this when merging windows logic is settled for
    # all possible merging situations.
    ix = context.get_state(self.INDEX_TAG)
    if ix < len(self.triggers):
      self.triggers[ix].on_merge(
          to_be_merged, merge_result, self._sub_context(context, ix))

  def should_fire(self, watermark, window, context):
    ix = context.get_state(self.INDEX_TAG)
    if ix < len(self.triggers):
      return self.triggers[ix].should_fire(
          watermark, window, self._sub_context(context, ix))

  def on_fire(self, watermark, window, context):
    ix = context.get_state(self.INDEX_TAG)
    if ix < len(self.triggers):
      if self.triggers[ix].on_fire(
          watermark, window, self._sub_context(context, ix)):
        ix += 1
        context.add_state(self.INDEX_TAG, ix)
      return ix == len(self.triggers)

  def reset(self, window, context):
    context.clear_state(self.INDEX_TAG)
    for ix, trigger in enumerate(self.triggers):
      trigger.reset(window, self._sub_context(context, ix))

  @staticmethod
  def _sub_context(context, index):
    return NestedContext(context, '%d/' % index)


class OrFinally(AfterFirst):

  def __init__(self, body_trigger, exit_trigger):
    super(OrFinally, self).__init__(body_trigger, exit_trigger)


class TriggerContext(object):

  def __init__(self, outer, window):
    self._outer = outer
    self._window = window

  # TODO(robertwb): Time domains.
  def set_timer(self, tag, timestamp):
    self._outer.set_timer(self._window, tag, timestamp)

  def clear_timer(self, timer):
    self._outer.clear_timer(self._window, timer)

  def add_state(self, tag, value):
    self._outer.add_state(self._window, tag, value)

  def get_state(self, tag):
    return self._outer.get_state(self._window, tag)

  def clear_state(self, tag):
    return self._outer.clear_state(self._window, tag)


class NestedContext(object):
  """Namespaced context useful for defining composite triggers."""

  def __init__(self, outer, prefix):
    self._outer = outer
    self._prefix = prefix

  def set_timer(self, tag, timestamp):
    self._outer.set_timer(self._prefix + tag, timestamp)

  def clear_timer(self, tag):
    self._outer.clear_timer(self._prefix + tag)

  def add_state(self, tag, value):
    self._outer.add_state(tag.with_prefix(self._prefix), value)

  def get_state(self, tag):
    return self._outer.get_state(tag.with_prefix(self._prefix))

  def clear_state(self, tag):
    self._outer.clear_state(tag.with_prefix(self._prefix))


# pylint: disable=unused-argument
class SimpleState(object):
  """Basic state storage interface used for triggering.

  Only timers must hold the watermark (by their timestamp).
  """

  __metaclass__ = ABCMeta

  @abstractmethod
  def set_timer(self, window, tag, timestamp):
    pass

  @abstractmethod
  def get_window(self, timer_id):
    pass

  @abstractmethod
  def clear_timer(self, window, timer):
    pass

  @abstractmethod
  def add_state(self, window, tag, value):
    pass

  @abstractmethod
  def get_state(self, window, tag):
    pass

  @abstractmethod
  def clear_state(self, window, tag):
    pass

  def at(self, window):
    return TriggerContext(self, window)


class UnmergedState(SimpleState):
  """State suitable for use in TriggerDriver.

  This class must be implemented by each backend.
  """

  @abstractmethod
  def set_global_state(self, tag, value):
    pass

  @abstractmethod
  def get_global_state(self, tag, default=None):
    pass
# pylint: enable=unused-argument


class MergeableStateAdapter(SimpleState):
  """Wraps a UnmergedState, tracking merged windows."""
  # TODO(robertwb): A similar indirection could be used for sliding windows
  # or other window_fns when a single element typically belongs to many windows.

  WINDOW_IDS = ValueStateTag('window_ids')

  def __init__(self, raw_state):
    self.raw_state = raw_state
    self.window_ids = self.raw_state.get_global_state(self.WINDOW_IDS, {})
    self.counter = None

  def set_timer(self, window, tag, timestamp):
    self.raw_state.set_timer(self._get_id(window), tag, timestamp)

  def clear_timer(self, window, timer):
    for window_id in self._get_ids(window):
      self.raw_state.clear_timer(window_id, timer)

  def add_state(self, window, tag, value):
    if isinstance(tag, ValueStateTag):
      raise ValueError(
          'Merging requested for non-mergeable state tag: %r.' % tag)
    self.raw_state.add_state(self._get_id(window), tag, value)

  def get_state(self, window, tag):
    values = [self.raw_state.get_state(window_id, tag)
              for window_id in self._get_ids(window)]
    if isinstance(tag, ValueStateTag):
      raise ValueError(
          'Merging requested for non-mergeable state tag: %r.' % tag)
    elif isinstance(tag, CombiningValueStateTag):
      # TODO(robertwb): Strip combine_fn.extract_output from raw_state tag.
      if not values:
        accumulator = tag.combine_fn.create_accumulator()
      elif len(values) == 1:
        accumulator = values[0]
      else:
        accumulator = tag.combine_fn.merge_accumulators(values)
        # TODO(robertwb): Store the merged value in the first tag.
      return tag.combine_fn.extract_output(accumulator)
    elif isinstance(tag, ListStateTag):
      return [v for vs in values for v in vs]
    else:
      raise ValueError('Invalid tag.', tag)

  def clear_state(self, window, tag):
    for window_id in self._get_ids(window):
      self.raw_state.clear_state(window_id, tag)
    if tag is None:
      del self.window_ids[window]
      self._persist_window_ids()

  def merge(self, to_be_merged, merge_result):
    for window in to_be_merged:
      if window != merge_result:
        if window in self.window_ids:
          if merge_result in self.window_ids:
            merge_window_ids = self.window_ids[merge_result]
          else:
            merge_window_ids = self.window_ids[merge_result] = []
          merge_window_ids.extend(self.window_ids.pop(window))
          self._persist_window_ids()

  def known_windows(self):
    return self.window_ids.keys()

  def get_window(self, timer_id):
    for window, ids in self.window_ids.items():
      if timer_id in ids:
        return window
    raise ValueError('No window for %s' % timer_id)

  def _get_id(self, window):
    if window in self.window_ids:
      return self.window_ids[window][0]
    else:
      window_id = self._get_next_counter()
      self.window_ids[window] = [window_id]
      self._persist_window_ids()
      return window_id

  def _get_ids(self, window):
    return self.window_ids.get(window, [])

  def _get_next_counter(self):
    if not self.window_ids:
      self.counter = 0
    elif self.counter is None:
      self.counter = max(k for ids in self.window_ids.values() for k in ids)
    self.counter += 1
    return self.counter

  def _persist_window_ids(self):
    self.raw_state.set_global_state(self.WINDOW_IDS, self.window_ids)

  def __repr__(self):
    return '\n\t'.join([repr(self.window_ids)] +
                       repr(self.raw_state).split('\n'))


def create_trigger_driver(windowing, is_batch=False):
  # TODO(robertwb): We can do more if we know elements are in timestamp
  # sorted order.
  if windowing.is_default() and is_batch:
    return DefaultGlobalBatchTriggerDriver()
  else:
    return GeneralTriggerDriver(windowing)


class TriggerDriver(object):
  """Breaks a series of bundle and timer firings into window (pane)s."""

  __metaclass__ = ABCMeta

  @abstractmethod
  def process_elements(self, windowed_values, state):
    pass

  @abstractmethod
  def process_timer(self, timer_id, timestamp, unused_tag, state):
    pass


class DefaultGlobalBatchTriggerDriver(TriggerDriver):
  """Breaks a bundles into window (pane)s according to the default triggering.
  """

  def __init__(self):
    pass

  def process_elements(self, windowed_values, state):
    if isinstance(windowed_values, list):
      unwindowed = [wv.value for wv in windowed_values]
    else:
      class UnwindowedValues(object):
        def __iter__(self):
          return (wv.value for wv in windowed_values)
      unwindowed = UnwindowedValues()
    yield GlobalWindow(), unwindowed

  def process_timer(self, timer_id, timestamp, unused_tag, state):
    raise TypeError('Triggers never set or called for batch default windowing.')


class GeneralTriggerDriver(TriggerDriver):
  """Breaks a series of bundle and timer firings into window (pane)s.

  Suitable for all variants of Windowing.
  """
  ELEMENTS = ListStateTag('elements')
  TOMBSTONE = CombiningValueStateTag('tombstone', combiners.CountCombineFn())

  def __init__(self, windowing):
    self.window_fn = windowing.windowfn
    self.trigger_fn = windowing.triggerfn
    self.accumulation_mode = windowing.accumulation_mode
    self.is_merging = True

  def process_elements(self, windowed_values, state):
    if self.is_merging:
      state = MergeableStateAdapter(state)

    windows_to_elements = collections.defaultdict(list)
    for wv in windowed_values:
      for window in wv.windows:
        windows_to_elements[window].append(wv.value)

    # First handle merging.
    if self.is_merging:
      old_windows = set(state.known_windows())
      all_windows = old_windows.union(windows_to_elements.keys())

      if all_windows != old_windows:
        merged_away = {}

        class TriggerMergeContext(WindowFn.MergeContext):

          def merge(_, to_be_merged, merge_result):
            for window in to_be_merged:
              if window != merge_result:
                merged_away[window] = merge_result
            state.merge(to_be_merged, merge_result)
            self.trigger_fn.on_merge(
                to_be_merged, merge_result, state.at(merge_result))

        self.window_fn.merge(TriggerMergeContext(all_windows))

        merged_windows_to_elements = collections.defaultdict(list)
        for window, values in windows_to_elements.items():
          while window in merged_away:
            window = merged_away[window]
          merged_windows_to_elements[window].extend(values)
        windows_to_elements = merged_windows_to_elements

    # Next handle element adding.
    for window, values in windows_to_elements.items():
      if state.get_state(window, self.TOMBSTONE):
        continue
      context = state.at(window)
      for value in values:
        state.add_state(window, self.ELEMENTS, value)
        self.trigger_fn.on_element(value, window, context)

      # Maybe fire this window.
      watermark = float('-inf')
      if self.trigger_fn.should_fire(watermark, window, context):
        finished = self.trigger_fn.on_fire(watermark, window, context)
        yield self._output(window, finished, state)

  def process_timer(self, timer_id, timestamp, unused_tag, state):
    if self.is_merging:
      state = MergeableStateAdapter(state)
    window = state.get_window(timer_id)
    if state.get_state(window, self.TOMBSTONE):
      return
    if not self.is_merging or window in state.known_windows():
      context = state.at(window)
      if self.trigger_fn.should_fire(timestamp, window, context):
        finished = self.trigger_fn.on_fire(timestamp, window, context)
        yield self._output(window, finished, state)

  def _output(self, window, finished, state):
    values = state.get_state(window, self.ELEMENTS)
    if finished:
      # TODO(robertwb): allowed lateness
      state.clear_state(window, None)
      state.add_state(window, self.TOMBSTONE, 1)
    elif self.accumulation_mode == AccumulationMode.DISCARDING:
      state.clear_state(window, self.ELEMENTS)
    return window, values


class InMemoryUnmergedState(UnmergedState):
  """In-memory implementation of UnmergedState.

  Used for batch and testing.
  """
  def __init__(self, defensive_copy=True):
    # TODO(robertwb): Skip defensive_copy in production if it's too expensive.
    self.timers = collections.defaultdict(dict)
    self.state = collections.defaultdict(lambda: collections.defaultdict(list))
    self.global_state = {}
    self.defensive_copy = defensive_copy

  def set_global_state(self, tag, value):
    assert isinstance(tag, ValueStateTag)
    if self.defensive_copy:
      value = copy.deepcopy(value)
    self.global_state[tag.tag] = value

  def get_global_state(self, tag, default=None):
    return self.global_state.get(tag.tag, default)

  def set_timer(self, window, tag, timestamp):
    self.timers[window][tag] = timestamp

  def clear_timer(self, window, tag):
    self.timers[window].pop(tag, None)

  def get_window(self, timer_id):
    return timer_id

  def add_state(self, window, tag, value):
    if self.defensive_copy:
      value = copy.deepcopy(value)
    if isinstance(tag, ValueStateTag):
      self.state[window][tag.tag] = value
    elif isinstance(tag, CombiningValueStateTag):
      self.state[window][tag.tag].append(value)
    elif isinstance(tag, ListStateTag):
      self.state[window][tag.tag].append(value)
    else:
      raise ValueError('Invalid tag.', tag)

  def get_state(self, window, tag):
    values = self.state[window][tag.tag]
    if isinstance(tag, ValueStateTag):
      return values
    elif isinstance(tag, CombiningValueStateTag):
      return tag.combine_fn.apply(values)
    elif isinstance(tag, ListStateTag):
      return values
    else:
      raise ValueError('Invalid tag.', tag)

  def clear_state(self, window, tag):
    if tag is None:
      self.state.pop(window, None)
    else:
      self.state[window].pop(tag.tag, None)

  def get_and_clear_timers(self, watermark=float('inf')):
    expired = []
    for window, timers in list(self.timers.items()):
      for tag, timestamp in list(timers.items()):
        if timestamp <= watermark:
          expired.append((window, (tag, timestamp)))
          del timers[tag]
      if not timers:
        del self.timers[window]
    return expired

  def __repr__(self):
    state_str = '\n'.join('%s: %s' % (key, dict(state))
                          for key, state in self.state.items())
    return 'timers: %s\nstate: %s' % (dict(self.timers), state_str)
