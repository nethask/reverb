# Lint as: python3
# Copyright 2019 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for tf_client."""

from concurrent import futures
import threading
import time

from absl.testing import parameterized
import numpy as np
from reverb import client as reverb_client
from reverb import distributions
from reverb import rate_limiters
from reverb import replay_sample
from reverb import server
from reverb import tf_client
import tensorflow.compat.v1 as tf
import tree


def make_server():
  return server.Server(
      priority_tables=[
          server.PriorityTable(
              'dist',
              sampler=distributions.Prioritized(priority_exponent=1),
              remover=distributions.Fifo(),
              max_size=1000000,
              rate_limiter=rate_limiters.MinSize(1)),
          server.PriorityTable(
              'dist2',
              sampler=distributions.Prioritized(priority_exponent=1),
              remover=distributions.Fifo(),
              max_size=1000000,
              rate_limiter=rate_limiters.MinSize(1)),
          server.PriorityTable(
              'signatured',
              sampler=distributions.Prioritized(priority_exponent=1),
              remover=distributions.Fifo(),
              max_size=1000000,
              rate_limiter=rate_limiters.MinSize(1),
              signature=tf.TensorSpec(dtype=tf.float32, shape=(None, None))),
      ],
      port=None,
  )


class SampleOpTest(tf.test.TestCase):

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls._server = make_server()
    cls._client = reverb_client.Client(f'localhost:{cls._server.port}')

  def tearDown(self):
    super().tearDown()
    self._client.reset('dist')

  @classmethod
  def tearDownClass(cls):
    super().tearDownClass()
    cls._server.stop()

  def testSample(self):
    input_data = [np.ones((81, 81), dtype=np.float64)]
    self._client.insert(input_data, {'dist': 1})
    with self.session() as session:
      client = tf_client.TFClient(self._client.server_address)
      sample = session.run(client.sample('dist', [tf.float64]))
      np.testing.assert_equal(input_data, sample.data)
      self.assertNotEqual(sample.info.key, 0)
      self.assertEqual(sample.info.probability, 1)
      self.assertEqual(sample.info.table_size, 1)

  def testSampleDtypeMismatchFails(self):
    data = [np.zeros((81, 81))]
    self._client.insert(data, {'dist': 1})
    with self.session() as session:
      client = tf_client.TFClient(self._client.server_address)
      with self.assertRaises(tf.errors.InternalError):
        session.run(client.sample('dist', [tf.float32]))

  def testSampleForwardServerError(self):
    with self.session() as session:
      client = tf_client.TFClient(self._client.server_address)
      with self.assertRaises(tf.errors.NotFoundError):
        session.run(client.sample('invalid', [tf.float64]))

  def testSampleRetryUntilOkOrFatal(self):
    with self.session() as session:
      client = tf_client.TFClient(self._client.server_address)
      with futures.ThreadPoolExecutor(max_workers=1) as executor:
        sample = executor.submit(session.run,
                                 client.sample('dist', [tf.float64]))
        input_data = [np.zeros((81, 81))]
        self._client.insert(input_data, {'dist': 1})
        np.testing.assert_equal(input_data, sample.result().data)


class UpdatePrioritiesOpTest(tf.test.TestCase):

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls._server = make_server()
    cls._client = reverb_client.Client(f'localhost:{cls._server.port}')

  def tearDown(self):
    super().tearDown()
    self._client.reset('dist')

  @classmethod
  def tearDownClass(cls):
    super().tearDownClass()
    cls._server.stop()

  def testUpdatePrioritiesShapeMismatchFails(self):
    with self.session() as session:
      client = tf_client.TFClient(self._client.server_address)
      update_op = client.update_priorities(
          tf.constant('dist'), tf.constant([1, 2], dtype=tf.uint64),
          tf.constant([1], dtype=tf.float64))
      with self.assertRaises(tf.errors.InvalidArgumentError):
        session.run(update_op)

  def testUpdatePriorities(self):
    with self.session() as session:
      client = tf_client.TFClient(self._client.server_address)
      update_op = client.update_priorities(
          tf.constant('dist'), tf.constant([1], dtype=tf.uint64),
          tf.constant([1], dtype=tf.float64))
      # TODO(b/154931002): Test that update is applied once Sample method is
      # exposed.
      self.assertEqual(None, session.run(update_op))


class InsertOpTest(tf.test.TestCase):

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls._server = make_server()
    cls._client = reverb_client.Client(f'localhost:{cls._server.port}')

  def tearDown(self):
    super().tearDown()
    self._client.reset('dist')
    self._client.reset('dist2')

  @classmethod
  def tearDownClass(cls):
    super().tearDownClass()
    cls._server.stop()

  def setUp(self):
    super().setUp()
    self.data = [tf.constant([1, 2, 3], dtype=tf.int8)]

  def testValidatesTablesHasRank1(self):
    client = tf_client.TFClient(self._client.server_address)
    priorities = tf.constant([1.0], dtype=tf.float64)

    # Works for rank 1.
    client.insert(self.data, tf.constant(['dist']), priorities)

    # Does not work for rank > 1.
    with self.assertRaises(ValueError):
      client.insert(self.data, tf.constant([['dist']]), priorities)

    # Does not work for rank < 1.
    with self.assertRaises(ValueError):
      client.insert(self.data, tf.constant('dist'), priorities)

  def testValidatesTablesDtype(self):
    client = tf_client.TFClient(self._client.server_address)
    with self.assertRaises(ValueError):
      client.insert(self.data, tf.constant([1]),
                    tf.constant([1.0], dtype=tf.float64))

  def testValidatesPrioritiesHasRank1(self):
    client = tf_client.TFClient(self._client.server_address)
    data = [tf.constant([1, 2])]
    tables = tf.constant(['dist'])

    # Works for rank 1.
    client.insert(data, tables, tf.constant([1.0], dtype=tf.float64))

    # Does not work for rank > 1.
    with self.assertRaises(ValueError):
      client.insert(data, tables, tf.constant([[1.0]], dtype=tf.float64))

    # Does not work for rank < 1.
    with self.assertRaises(ValueError):
      client.insert(data, tables, tf.constant(1.0, dtype=tf.float64))

  def testValidatesPrioritiesDtype(self):
    client = tf_client.TFClient(self._client.server_address)
    with self.assertRaises(ValueError):
      client.insert(self.data, tf.constant(['dist']),
                    tf.constant([1.0], dtype=tf.float32))

  def testValidatesTablesAndPrioritiesHaveSameShape(self):
    client = tf_client.TFClient(self._client.server_address)
    with self.assertRaises(ValueError):
      client.insert(self.data, tf.constant(['dist', 'dist2']),
                    tf.constant([1.0], dtype=tf.float64))

  def testInsertSingleTable(self):
    with self.session() as session:
      client = tf_client.TFClient(self._client.server_address)
      insert_op = client.insert(
          data=[tf.constant([1, 2, 3], dtype=tf.int8)],
          tables=tf.constant(['dist']),
          priorities=tf.constant([1.0], dtype=tf.float64))
      sample_op = client.sample('dist', [tf.int8])

      # Check that insert op succeeds.
      self.assertEqual(None, session.run(insert_op))

      # Check that the sampled data matches the inserted.
      sample = session.run(sample_op)
      self.assertLen(sample.data, 1)
      np.testing.assert_equal(
          np.array([1, 2, 3], dtype=np.int8), sample.data[0])

  def testInsertMultiTable(self):
    with self.session() as session:
      client = tf_client.TFClient(self._client.server_address)
      insert_op = client.insert(
          data=[tf.constant([1, 2, 3], dtype=tf.int8)],
          tables=tf.constant(['dist', 'dist2']),
          priorities=tf.constant([1.0, 2.0], dtype=tf.float64))

      sample_ops = [
          client.sample('dist', [tf.int8]),
          client.sample('dist2', [tf.int8])
      ]

      # Check that insert op succeeds.
      self.assertEqual(None, session.run(insert_op))

      # Check that the sampled data matches the inserted in all tables.
      for sample_op in sample_ops:
        sample = session.run(sample_op)
        self.assertLen(sample.data, 1)
        np.testing.assert_equal(
            np.array([1, 2, 3], dtype=np.int8), sample.data[0])


class DatasetTest(tf.test.TestCase, parameterized.TestCase):

  @classmethod
  def setUpClass(cls):
    super().setUpClass()
    cls._server = make_server()
    cls._client = reverb_client.Client(f'localhost:{cls._server.port}')

  def tearDown(self):
    super().tearDown()
    self._client.reset('dist')
    self._client.reset('signatured')

  @classmethod
  def tearDownClass(cls):
    super().tearDownClass()
    cls._server.stop()

  def _PopulateReplay(self, sequence_length=100, max_time_steps=None):
    max_time_steps = max_time_steps or sequence_length
    with self._client.writer(max_time_steps) as writer:
      for i in range(1000):
        writer.append_timestep([np.zeros((3, 3), dtype=np.float32)])
        if i % 5 == 0 and i >= sequence_length:
          writer.create_prioritized_item(
              table='dist', num_timesteps=sequence_length, priority=1)
          writer.create_prioritized_item(
              table='signatured', num_timesteps=sequence_length, priority=1)

  def _SampleFrom(self, dataset, num_samples):
    iterator = dataset.make_initializable_iterator()
    dataset_item = iterator.get_next()
    self.evaluate(iterator.initializer)
    return [self.evaluate(dataset_item) for _ in range(num_samples)]

  @parameterized.named_parameters(
      {
          'testcase_name': 'default_values',
      },
      {
          'testcase_name': 'num_workers_per_iterator_is_0',
          'num_workers_per_iterator': 0,
          'want_error': ValueError,
      },
      {
          'testcase_name': 'num_workers_per_iterator_is_1',
          'num_workers_per_iterator': 1,
      },
      {
          'testcase_name': 'num_workers_per_iterator_is_minus_1',
          'num_workers_per_iterator': -1,
      },
      {
          'testcase_name': 'num_workers_per_iterator_is_minus_2',
          'num_workers_per_iterator': -2,
          'want_error': ValueError,
      },
      {
          'testcase_name': 'max_samples_per_stream_is_0',
          'max_samples_per_stream': 0,
          'want_error': ValueError,
      },
      {
          'testcase_name': 'max_samples_per_stream_is_1',
          'max_samples_per_stream': 1,
      },
      {
          'testcase_name': 'max_samples_per_stream_is_minus_1',
          'max_samples_per_stream': -1,
      },
      {
          'testcase_name': 'max_samples_per_stream_is_minus_2',
          'num_workers_per_iterator': -2,
          'want_error': ValueError,
      },
      {
          'testcase_name': 'capacity_is_0',
          'capacity': 0,
          'want_error': ValueError,
      },
      {
          'testcase_name': 'capacity_is_1',
          'capacity': 1,
      },
      {
          'testcase_name': 'capacity_is_minus_1',
          'capacity': -1,
          'want_error': ValueError,
      },
  )
  def testSamplerParametersValidation(self, **kwargs):
    client = tf_client.TFClient(self._client.server_address)
    dtypes = (tf.float32,)
    shapes = (tf.TensorShape([3, 3]),)

    if 'want_error' in kwargs:
      error = kwargs.pop('want_error')
      with self.assertRaises(error):
        client.dataset('dist', dtypes, shapes, **kwargs)
    else:
      client.dataset('dist', dtypes, shapes, **kwargs)

  def testIterate(self):
    self._PopulateReplay()

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='dist', dtypes=(tf.float32,), shapes=(tf.TensorShape([3, 3]),))
    got = self._SampleFrom(dataset, 10)
    for sample in got:
      self.assertIsInstance(sample, replay_sample.ReplaySample)
      # A single sample is returned so the key should be a scalar int64.
      self.assertIsInstance(sample.info.key, np.uint64)
      np.testing.assert_array_equal(sample.data[0],
                                    np.zeros((3, 3), dtype=np.float32))

  def testInconsistentSignatureSize(self):
    self._PopulateReplay()

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='signatured',
        dtypes=(tf.float32, tf.float64),
        shapes=(tf.TensorShape([3, 3]), tf.TensorShape([])))
    with self.assertRaisesWithPredicateMatch(
        tf.errors.InvalidArgumentError,
        r'Inconsistent number of tensors requested from table \'signatured\'.  '
        r'Requested 5 tensors, but table signature shows 4 tensors.'):
      self._SampleFrom(dataset, 10)

  def testIncompatibleSignatureDtype(self):
    self._PopulateReplay()

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='signatured',
        dtypes=(tf.int64,),
        shapes=(tf.TensorShape([3, 3]),))
    with self.assertRaisesWithPredicateMatch(
        tf.errors.InvalidArgumentError,
        r'Requested incompatible tensor at flattened index 3 from table '
        r'\'signatured\'.  Requested \(dtype, shape\): \(int64, \[3,3\]\).  '
        r'Signature \(dtype, shape\): \(float, \[\?,\?\]\)'):
      self._SampleFrom(dataset, 10)

  def testIncompatibleSignatureShape(self):
    self._PopulateReplay()

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='signatured', dtypes=(tf.float32,), shapes=(tf.TensorShape([3]),))
    with self.assertRaisesWithPredicateMatch(
        tf.errors.InvalidArgumentError,
        r'Requested incompatible tensor at flattened index 3 from table '
        r'\'signatured\'.  Requested \(dtype, shape\): \(float, \[3\]\).  '
        r'Signature \(dtype, shape\): \(float, \[\?,\?\]\)'):
      self._SampleFrom(dataset, 10)

  @parameterized.parameters([1], [3], [10])
  def testIncompatibleShapeWhenUsingSequenceLength(self, sequence_length):
    client = tf_client.TFClient(self._client.server_address)
    with self.assertRaises(ValueError):
      client.dataset(
          table='dist',
          dtypes=(tf.float32,),
          shapes=(tf.TensorShape([sequence_length + 1, 3, 3]),),
          emit_timesteps=False,
          sequence_length=sequence_length)

  @parameterized.parameters(
      ('dist', 1, 1),
      ('dist', 1, 3),
      ('dist', 3, 3),
      ('dist', 3, 5),
      ('dist', 10, 10),
      ('dist', 10, 11),
      ('signatured', 1, 1),
      ('signatured', 3, 3),
      ('signatured', 3, 5),
      ('signatured', 10, 10),
  )
  def testIterateWithSequenceLength(
      self, table_name, sequence_length, max_time_steps):
    # Also ensure we get sequence_length-shaped outputs when
    # writers' max_time_steps != sequence_length.
    self._PopulateReplay(sequence_length, max_time_steps=max_time_steps)

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table=table_name,
        dtypes=(tf.float32,),
        shapes=(tf.TensorShape([sequence_length, 3, 3]),),
        emit_timesteps=False,
        sequence_length=sequence_length)

    got = self._SampleFrom(dataset, 10)
    for sample in got:
      self.assertIsInstance(sample, replay_sample.ReplaySample)

      # The keys and data should be batched up by the sequence length.
      self.assertEqual(sample.info.key.shape, (sequence_length,))
      np.testing.assert_array_equal(
          sample.data[0], np.zeros((sequence_length, 3, 3), dtype=np.float32))

  @parameterized.parameters(
      ('dist', 1),
      ('dist', 3),
      ('dist', 10),
      ('signatured', 1),
      ('signatured', 3),
      ('signatured', 10),
  )
  def testIterateWithUnknownSequenceLength(self, table_name, sequence_length):
    self._PopulateReplay(sequence_length)

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table=table_name,
        dtypes=(tf.float32,),
        shapes=(tf.TensorShape([None, 3, 3]),),
        emit_timesteps=False,
        sequence_length=None)

    # Check the shape of the items.
    iterator = dataset.make_initializable_iterator()
    dataset_item = iterator.get_next()
    self.assertIsNone(dataset_item.info.key.shape.as_list()[0], None)
    self.assertIsNone(dataset_item.data[0].shape.as_list()[0], None)

    # Verify that once evaluated, the samples has the expected length.
    got = self._SampleFrom(dataset, 10)
    for sample in got:
      self.assertIsInstance(sample, replay_sample.ReplaySample)

      # The keys and data should be batched up by the sequence length.
      self.assertEqual(sample.info.key.shape, (sequence_length,))
      np.testing.assert_array_equal(
          sample.data[0], np.zeros((sequence_length, 3, 3), dtype=np.float32))

  @parameterized.parameters(
      ('dist', 1, 2),
      ('dist', 2, 1),
      ('signatured', 1, 2),
      ('signatured', 2, 1),
  )
  def testValidatesSequenceLengthWhenTimestepsEmitted(self, table_name,
                                                      actual_sequence_length,
                                                      provided_sequence_length):
    self._PopulateReplay(actual_sequence_length)

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table=table_name,
        dtypes=(tf.float32,),
        shapes=(tf.TensorShape([provided_sequence_length, 3, 3]),),
        emit_timesteps=True,
        sequence_length=provided_sequence_length)

    with self.assertRaises(tf.errors.InvalidArgumentError):
      self._SampleFrom(dataset, 10)

  @parameterized.named_parameters(
      dict(testcase_name='TableDist', table_name='dist'),
      dict(testcase_name='TableSignatured', table_name='signatured'))
  def testIterateBatched(self, table_name):
    self._PopulateReplay()

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table=table_name,
        dtypes=(tf.float32,),
        shapes=(tf.TensorShape([3, 3]),))
    dataset = dataset.batch(2, True)

    got = self._SampleFrom(dataset, 10)
    for sample in got:
      self.assertIsInstance(sample, replay_sample.ReplaySample)

      # The keys should be batched up like the data.
      self.assertEqual(sample.info.key.shape, (2,))

      np.testing.assert_array_equal(sample.data[0],
                                    np.zeros((2, 3, 3), dtype=np.float32))

  def testIterateNestedAndBatched(self):
    with self._client.writer(100) as writer:
      for i in range(1000):
        writer.append_timestep({
            'observation': {
                'data': np.zeros((3, 3), dtype=np.float32),
                'extras': [
                    np.int64(10),
                    np.ones([1], dtype=np.int32),
                ],
            },
            'reward': np.zeros((10, 10), dtype=np.float32),
        })
        if i % 5 == 0 and i >= 100:
          writer.create_prioritized_item(
              table='dist', num_timesteps=100, priority=1)

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='dist',
        dtypes=(((tf.float32), (tf.int64, tf.int32)), tf.float32),
        shapes=((tf.TensorShape([3, 3]), (tf.TensorShape(None),
                                          tf.TensorShape([1]))),
                tf.TensorShape([10, 10])),
    )
    dataset = dataset.batch(3)

    structure = {
        'observation': {
            'data':
                tf.TensorSpec([3, 3], tf.float32),
            'extras': [
                tf.TensorSpec([], tf.int64),
                tf.TensorSpec([1], tf.int32),
            ],
        },
        'reward': tf.TensorSpec([], tf.int64),
    }

    got = self._SampleFrom(dataset, 10)
    self.assertLen(got, 10)
    for sample in got:
      self.assertIsInstance(sample, replay_sample.ReplaySample)

      transition = tree.unflatten_as(structure, tree.flatten(sample.data))
      np.testing.assert_array_equal(transition['observation']['data'],
                                    np.zeros([3, 3, 3], dtype=np.float32))
      np.testing.assert_array_equal(transition['observation']['extras'][0],
                                    np.ones([3], dtype=np.int64) * 10)
      np.testing.assert_array_equal(transition['observation']['extras'][1],
                                    np.ones([3, 1], dtype=np.int32))
      np.testing.assert_array_equal(transition['reward'],
                                    np.zeros([3, 10, 10], dtype=np.float32))

  def testMultipleIterators(self):
    with self._client.writer(100) as writer:
      for i in range(10):
        writer.append_timestep([np.ones((81, 81), dtype=np.float32) * i])
      writer.create_prioritized_item(table='dist', num_timesteps=10, priority=1)

    trajectory_length = 5
    batch_size = 3

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='dist', dtypes=(tf.float32,), shapes=(tf.TensorShape([81, 81]),))
    dataset = dataset.batch(trajectory_length)

    iterators = [
        dataset.make_initializable_iterator() for _ in range(batch_size)
    ]
    items = tf.stack(
        [tf.squeeze(iterator.get_next().data) for iterator in iterators])

    with self.session() as session:
      session.run([iterator.initializer for iterator in iterators])
      got = session.run(items)
      self.assertEqual(got.shape, (batch_size, trajectory_length, 81, 81))

      want = np.array(
          [[np.ones([81, 81]) * i for i in range(trajectory_length)]] *
          batch_size)
      np.testing.assert_array_equal(got, want)

  def testIterateOverBlobs(self):
    for _ in range(10):
      self._client.insert((np.ones([3, 3], dtype=np.int32)), {'dist': 1})

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='dist', dtypes=(tf.int32,), shapes=(tf.TensorShape([3, 3]),))

    got = self._SampleFrom(dataset, 20)
    self.assertLen(got, 20)
    for sample in got:
      self.assertIsInstance(sample, replay_sample.ReplaySample)
      self.assertIsInstance(sample.info.key, np.uint64)
      self.assertIsInstance(sample.info.probability, np.float64)
      np.testing.assert_array_equal(sample.data[0],
                                    np.ones((3, 3), dtype=np.int32))

  def testIterateOverBatchedBlobs(self):
    for _ in range(10):
      self._client.insert((np.ones([3, 3], dtype=np.int32)), {'dist': 1})

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='dist', dtypes=(tf.int32,), shapes=(tf.TensorShape([3, 3]),))

    dataset = dataset.batch(5)

    got = self._SampleFrom(dataset, 20)
    self.assertLen(got, 20)
    for sample in got:
      self.assertIsInstance(sample, replay_sample.ReplaySample)
      self.assertEqual(sample.info.key.shape, (5,))
      np.testing.assert_array_equal(sample.data[0],
                                    np.ones((5, 3, 3), dtype=np.int32))

  def testConvertsSpecListsIntoTuples(self):
    for _ in range(10):
      data = [
          (np.ones([1, 1], dtype=np.int32),),
          [
              np.ones([3, 3], dtype=np.int8),
              (np.ones([2, 2], dtype=np.float64),)
          ],
      ]
      self._client.insert(data, {'dist': 1})

    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='dist',
        dtypes=[
            (tf.int32,),
            [
                tf.int8,
                (tf.float64,),
            ],
        ],
        shapes=[
            (tf.TensorShape([1, 1]),),
            [
                tf.TensorShape([3, 3]),
                (tf.TensorShape([2, 2]),),
            ],
        ])

    got = self._SampleFrom(dataset, 10)

    for sample in got:
      self.assertIsInstance(sample, replay_sample.ReplaySample)
      self.assertIsInstance(sample.info.key, np.uint64)
      tree.assert_same_structure(sample.data, (
          (None,),
          (
              None,
              (None,),
          ),
      ))

  def testSessionIsClosedWhileOpPending(self):
    client = tf_client.TFClient(self._client.server_address)
    dataset = client.dataset(
        table='dist', dtypes=tf.float32, shapes=tf.TensorShape([]))

    iterator = dataset.make_initializable_iterator()
    item = iterator.get_next()

    def _session_closer(sess, wait_time_secs):
      def _fn():
        time.sleep(wait_time_secs)
        sess.close()

      return _fn

    with self.session() as sess:
      sess.run(iterator.initializer)
      thread = threading.Thread(target=_session_closer(sess, 3))
      thread.start()
      with self.assertRaises(tf.errors.CancelledError):
        sess.run(item)


if __name__ == '__main__':
  tf.disable_eager_execution()
  tf.test.main()