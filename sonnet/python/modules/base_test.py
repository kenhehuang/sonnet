# Copyright 2017 The Sonnet Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or  implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================

"""Tests for sonnet.python.modules.base."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import inspect
import pickle

# Dependency imports
from absl.testing import parameterized
import numpy as np
import six
from sonnet.python.modules import base
import tensorflow as tf

tfe = tf.contrib.eager
logging = tf.logging


class ModuleWithClassKeys(base.AbstractModule):
  """Dummy module that defines some keys as class attributes."""
  POSSIBLE_INITIALIZER_KEYS = {"foo", "bar"}


class ModuleWithNoInitializerKeys(base.AbstractModule):
  """Dummy module without any intiailizer keys."""
  pass


class ModuleWithCustomInitializerKeys(base.AbstractModule):
  """Dummy module that overrides get_possible_initializer_keys."""

  @classmethod
  def get_possible_initializer_keys(cls, custom_key):
    return {"foo"} if custom_key else {"bar"}


class IdentityModule(base.AbstractModule):
  """Sonnet module that builds a single `tf.identity` op."""

  def _build(self, inputs):
    return tf.identity(inputs)


class NoInitIdentityModule(base.AbstractModule):
  """Sonnet module that inherits `base.AbstractModule.__init__`."""

  def _build(self, inputs):
    return tf.identity(inputs)


class NoSuperInitIdentityModule(base.AbstractModule):
  """Sonnet module that doesn't call `base.AbstractModule.__init__`."""

  def __init__(self):
    pass  # Don't call superclass initializer.

  def _build(self, inputs):
    return tf.identity(inputs)


class SimpleModule(base.AbstractModule):
  """Simple module with variables created in constructor and build."""

  def __init__(self, custom_getter=None, name="simple_module"):

    super(SimpleModule, self).__init__(custom_getter=custom_getter,
                                       name=name)

    with self._enter_variable_scope():
      self._b = tf.get_variable("b", dtype=tf.float32, shape=[10, 10])

  def _build(self, inputs):
    """Connect a simple module to the graph."""
    self._w = tf.get_variable("w", dtype=tf.float32, shape=[10, 10])

    return self._w * inputs + self._b


class ComplexModule(base.AbstractModule):
  """Complex module consisting of two sub modules."""

  def __init__(self, custom_getter=None, name="complex_module"):

    super(ComplexModule, self).__init__(custom_getter=custom_getter,
                                        name=name)

    with self._enter_variable_scope():
      self._a = SimpleModule(name="linear_1")

  def _build(self, inputs):
    self._b = SimpleModule(name="linear_2")

    return self._b(self._a(inputs))  # pylint: disable=not-callable


class ModuleWithSubmodules(base.AbstractModule):

  def __init__(self,
               submodule_a,
               submodule_b,
               custom_getter=None,
               name="module_with_submodules"):
    super(ModuleWithSubmodules, self).__init__(
        custom_getter=custom_getter, name=name)

    self._submodule_a = submodule_a
    self._submodule_b = submodule_b

  def _build(self, inputs):
    c = SimpleModule(name="simple_build")
    d = ComplexModule(name="complex_build")
    return d(self._submodule_a(inputs)) +  self._submodule_b(c(inputs))  # pylint: disable=not-callable


# @tf.contrib.eager.run_all_tests_in_graph_and_eager_modes
class AbstractModuleTest(parameterized.TestCase, tf.test.TestCase):

  def testInitializerKeys(self):
    keys = ModuleWithClassKeys.get_possible_initializer_keys()
    self.assertEqual(keys, {"foo", "bar"})
    keys = ModuleWithNoInitializerKeys.get_possible_initializer_keys()
    self.assertEqual(keys, set())
    msg = ("missing 1 required positional argument" if six.PY3
           else "takes exactly 2 arguments")
    self.assertRaisesRegexp(
        TypeError, msg,
        ModuleWithCustomInitializerKeys.get_possible_initializer_keys)
    keys = ModuleWithCustomInitializerKeys.get_possible_initializer_keys(True)
    self.assertEqual(keys, {"foo"})
    keys = ModuleWithCustomInitializerKeys.get_possible_initializer_keys(False)
    self.assertEqual(keys, {"bar"})

  def testMultipleGraphs(self):
    id_mod = IdentityModule(name="identity")
    # gpylint incorrectly thinks IdentityModule is not callable, so disable.
    # pylint: disable=not-callable
    with tf.Graph().as_default() as graph:
      id_mod(tf.ones(dtype=tf.float32, shape=[42]))
      self.assertEqual(id_mod._graph, graph)

    with tf.Graph().as_default():
      with self.assertRaisesRegexp(base.DifferentGraphError,
                                   "Cannot connect module"):
        id_mod(tf.ones(dtype=tf.float32, shape=[42]))
    # pylint: enable=not-callable

  def testNameScopeRecording(self):
    if tf.executing_eagerly():
      self.skipTest("Name scopes are not recorded in eager mode.")

    id_mod = IdentityModule(name="foo")

    # Connect inside different name scope contexts, check that each is recorded.
    # pylint: disable=not-callable
    id_mod(tf.ones(dtype=tf.float32, shape=[22]))
    self.assertIn(id_mod.name_scopes, (("foo",), ("foo_1",)))
    with tf.name_scope("blah"):
      id_mod(tf.ones(dtype=tf.float32, shape=[23]))
    self.assertIn(id_mod.name_scopes,
                  (("foo", "blah/foo"), ("foo_1", "blah/foo")))
    with tf.name_scope("baz"):
      id_mod(tf.ones(dtype=tf.float32, shape=[24]))
    # pylint: enable=not-callable
    self.assertIn(id_mod.name_scopes,
                  (("foo", "blah/foo", "baz/foo"),
                   ("foo_1", "blah/foo", "baz/foo")))

  def testNameScopeRecordingNotSupportedEager(self):
    if not tf.executing_eagerly():
      self.skipTest("Name scopes are recorded in graph mode.")

    id_mod = IdentityModule(name="foo")
    id_mod(tf.ones(dtype=tf.float32, shape=[22]))
    with self.assertRaisesRegexp(base.NotSupportedError,
                                 "not supported in eager"):
      id_mod.name_scopes  # pylint: disable=pointless-statement

  def testSubgraphsRecording(self):
    if tf.executing_eagerly():
      self.skipTest("Subgraphs are not recorded in eager mode.")

    id_mod = IdentityModule(name="foo")

    with self.assertRaisesRegexp(base.NotConnectedError,
                                 "not instantiated yet"):
      id_mod.last_connected_subgraph()

    # pylint: disable=not-callable
    inputs = tf.ones(dtype=tf.float32, shape=[21])
    outputs = id_mod(inputs)
    with tf.name_scope("blah"):
      blah_inputs = tf.ones(dtype=tf.float32, shape=[22])
      blah_outputs = id_mod(blah_inputs)
    with tf.name_scope("baz"):
      baz_inputs = tf.ones(dtype=tf.float32, shape=[23])
      baz_outputs = id_mod(baz_inputs)
    # pylint: enable=not-callable
    subgraphs = id_mod.connected_subgraphs
    self.assertEqual(id_mod.last_connected_subgraph.name_scope, "baz/foo")
    self.assertIs(id_mod.last_connected_subgraph, subgraphs[2])
    self.assertIs(subgraphs[0].module, id_mod)
    self.assertIn(subgraphs[0].name_scope, ("foo", "foo_1"))
    self.assertEqual(subgraphs[1].name_scope, "blah/foo")
    self.assertEqual(subgraphs[2].name_scope, "baz/foo")
    self.assertIs(subgraphs[0].inputs["inputs"], inputs)
    self.assertIs(subgraphs[1].inputs["inputs"], blah_inputs)
    self.assertIs(subgraphs[2].inputs["inputs"], baz_inputs)
    self.assertIs(subgraphs[0].outputs, outputs)
    self.assertIs(subgraphs[1].outputs, blah_outputs)
    self.assertIs(subgraphs[2].outputs, baz_outputs)

  def testSubgraphsNotRecordedEager(self):
    if not tf.executing_eagerly():
      self.skipTest("Subgraphs are recorded in graph mode")

    id_mod = IdentityModule(name="foo")

    with self.assertRaisesRegexp(base.NotSupportedError,
                                 "not tracked in eager mode"):
      id_mod.last_connected_subgraph()

    # pylint: disable=not-callable
    inputs = tf.ones(dtype=tf.float32, shape=[21])
    id_mod(inputs)
    with tf.name_scope("blah"):
      blah_inputs = tf.ones(dtype=tf.float32, shape=[22])
      id_mod(blah_inputs)
    with tf.name_scope("baz"):
      baz_inputs = tf.ones(dtype=tf.float32, shape=[23])
      id_mod(baz_inputs)
    # pylint: enable=not-callable

    with self.assertRaisesRegexp(base.NotSupportedError,
                                 "not tracked in eager mode"):
      id_mod.connected_subgraphs  # pylint: disable=pointless-statement

  def testInitNoNamedArgs(self):
    """Tests if calling __init__ without named args raises a ValueError."""
    with self.assertRaises(ValueError):
      NoInitIdentityModule("foobar")

  def testInitInvalidTypeArgs(self):
    """Tests if calling __init__ without a string name raises a TypeError."""
    with self.assertRaises(TypeError):
      NoInitIdentityModule(name=123)

  def testInitNoArgs(self):
    """Tests if calling __init__ with no args uses correct defaults."""
    module = NoInitIdentityModule()
    self.assertEqual(module.module_name, "no_init_identity_module")

  def testInitNoSuper(self):
    """Tests if a __call__ with no __init__ raises an error."""
    module = NoSuperInitIdentityModule()
    with self.assertRaises(base.NotInitializedError):
      module(tf.constant([1]))  # pylint: disable=not-callable

  def testPicklingNotSupported(self):
    module = IdentityModule()
    with self.assertRaisesRegexp(base.NotSupportedError,
                                 "cannot be serialized"):
      # Writing the object to a string will fail.
      pickle.dumps(module)

  def testCustomGetter(self):

    connection_count = {"x": 0}
    def custom_getter(getter, name, *args, **kwargs):
      connection_count["x"] += 1
      return getter(name, *args, **kwargs)

    inputs = tf.ones(dtype=tf.float32, shape=[10, 10])

    with tf.variable_scope("scope"):
      module = SimpleModule(name="mod1")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(0, connection_count["x"])

      module = SimpleModule(custom_getter=custom_getter, name="mod2")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(2, connection_count["x"])  # w & b

      module = SimpleModule(custom_getter={"w": custom_getter}, name="mod3")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(3, connection_count["x"])  # w

      module = SimpleModule(custom_getter={"w.*": custom_getter}, name="mod3")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(4, connection_count["x"])  # w

      module = SimpleModule(custom_getter={".*": custom_getter}, name="mod4")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(6, connection_count["x"])  # w & b

      err = r"More than one custom_getter matched scope/mod5/w \(w\):.*"
      with self.assertRaisesRegexp(KeyError, err):
        module = SimpleModule(
            custom_getter={".*": custom_getter, "w.*": custom_getter},
            name="mod5")
        module(inputs)  # pylint: disable=not-callable

      err = "Given custom_getter is not callable."
      with self.assertRaisesRegexp(TypeError, err):
        module = SimpleModule(custom_getter=0, name="mod6")
      with self.assertRaisesRegexp(TypeError, err):
        module = SimpleModule(custom_getter={"w": 0}, name="mod7")

  def testCustomGetterNested(self):

    def custom_getter(getter, name, *args, **kwargs):
      kwargs["trainable"] = False
      return getter(name, *args, **kwargs)

    inputs = tf.ones(dtype=tf.float32, shape=[10, 10])

    with tf.variable_scope("scope"):
      module = ComplexModule(name="mod1")
      module(inputs)  # pylint: disable=not-callable
      self.assertLen(tf.trainable_variables(), 4)

      module = ComplexModule(custom_getter=custom_getter, name="mod2")
      module(inputs)  # pylint: disable=not-callable
      self.assertLen(tf.trainable_variables(), 4)  # All variables.

      module = ComplexModule(custom_getter={".*/w": custom_getter},
                             name="mod3")
      module(inputs)  # pylint: disable=not-callable
      trainable_names = [v.name for v in tf.trainable_variables()]
      self.assertLen(trainable_names, 6)  # linear_1/w and linear_2/w.
      self.assertIn("scope/mod3/linear_1/b:0", trainable_names)
      self.assertIn("scope/mod3/linear_2/b:0", trainable_names)

      module = ComplexModule(custom_getter={".*/b": custom_getter}, name="mod4")
      module(inputs)  # pylint: disable=not-callable
      trainable_names = [v.name for v in tf.trainable_variables()]
      self.assertLen(trainable_names, 8)  # linear_1/b and linear_2/b.
      self.assertIn("scope/mod4/linear_1/w:0", trainable_names)
      self.assertIn("scope/mod4/linear_2/w:0", trainable_names)

      module = ComplexModule(custom_getter={".*": custom_getter}, name="mod5")
      module(inputs)  # pylint: disable=not-callable
      self.assertLen(tf.trainable_variables(), 8)  # All variables.

      module = ComplexModule(custom_getter={"w": custom_getter}, name="mod6")
      module(inputs)  # pylint: disable=not-callable
      self.assertLen(tf.trainable_variables(), 12)  # No variables.

  @parameterized.parameters(
      [lambda m: m.get_all_variables(),
       lambda m: m.variables,
       lambda m: m.trainable_variables]
  )
  def testGetAllTrainableVariables(self, all_trainable_variables):
    inputs = tf.ones(dtype=tf.float32, shape=[10, 10])
    submodule_a = SimpleModule(name="simple_submodule")
    submodule_b = ComplexModule(name="complex_submodule")
    module = ModuleWithSubmodules(
        submodule_a=submodule_a, submodule_b=submodule_b)
    with self.assertRaisesRegexp(base.NotConnectedError,
                                 "not instantiated yet"):
      all_trainable_variables(module)
    module(inputs)  # pylint: disable=not-callable

    # Check correct for SimpleModule.
    submodule_a_variables = submodule_a.get_variables()
    submodule_a_variable_names = sorted(
        [str(v.name) for v in submodule_a_variables])
    submodule_a_all_variables = all_trainable_variables(submodule_a)
    submodule_a_all_variable_names = sorted(
        [str(v.name) for v in submodule_a_all_variables])
    self.assertEqual(submodule_a_variable_names, submodule_a_all_variable_names)
    self.assertEqual([
        "simple_submodule/b:0",
        "simple_submodule/w:0",
    ], submodule_a_variable_names)

    # Check correct for ComplexModule
    submodule_b_variables = all_trainable_variables(submodule_b)
    submodule_b_variable_names = sorted(
        [str(v.name) for v in submodule_b_variables])
    self.assertEqual([
        "complex_submodule/linear_1/b:0",
        "complex_submodule/linear_1/w:0",
        "complex_submodule/linear_2/b:0",
        "complex_submodule/linear_2/w:0",
    ], submodule_b_variable_names)

    all_variables = all_trainable_variables(module)
    all_variable_names = sorted([str(v.name) for v in all_variables])
    self.assertEqual([
        "complex_submodule/linear_1/b:0",
        "complex_submodule/linear_1/w:0",
        "complex_submodule/linear_2/b:0",
        "complex_submodule/linear_2/w:0",
        "module_with_submodules/complex_build/linear_1/b:0",
        "module_with_submodules/complex_build/linear_1/w:0",
        "module_with_submodules/complex_build/linear_2/b:0",
        "module_with_submodules/complex_build/linear_2/w:0",
        "module_with_submodules/simple_build/b:0",
        "module_with_submodules/simple_build/w:0",
        "simple_submodule/b:0",
        "simple_submodule/w:0",
    ], all_variable_names)

    self.assertEmpty(
        module.get_all_variables(collection=tf.GraphKeys.LOCAL_VARIABLES))

    # Create another ModuleWithSubmodules with the same submodules
    module = ModuleWithSubmodules(
        submodule_a=submodule_a, submodule_b=submodule_b)
    module(inputs)  # pylint: disable=not-callable

    all_variables = all_trainable_variables(module)
    all_variable_names = sorted([str(v.name) for v in all_variables])
    self.assertEqual([
        "complex_submodule/linear_1/b:0",
        "complex_submodule/linear_1/w:0",
        "complex_submodule/linear_2/b:0",
        "complex_submodule/linear_2/w:0",
        "module_with_submodules_1/complex_build/linear_1/b:0",
        "module_with_submodules_1/complex_build/linear_1/w:0",
        "module_with_submodules_1/complex_build/linear_2/b:0",
        "module_with_submodules_1/complex_build/linear_2/w:0",
        "module_with_submodules_1/simple_build/b:0",
        "module_with_submodules_1/simple_build/w:0",
        "simple_submodule/b:0",
        "simple_submodule/w:0",
    ], all_variable_names)

  @parameterized.parameters(
      [lambda m: m.get_all_variables(tf.GraphKeys.LOCAL_VARIABLES),
       lambda m: m.non_trainable_variables])
  def testGetAllLocalVariables(self, get_non_trainable_variables):
    def local_custom_getter(getter, *args, **kwargs):
      kwargs["trainable"] = False
      if "collections" in kwargs and kwargs["collections"] is not None:
        kwargs["collections"] += [tf.GraphKeys.LOCAL_VARIABLES]
      else:
        kwargs["collections"] = [tf.GraphKeys.LOCAL_VARIABLES]
      return getter(*args, **kwargs)

    inputs = tf.ones(dtype=tf.float32, shape=[10, 10])
    # Create a new ModuleWithSubmodules that uses all local variables
    with tf.variable_scope("", custom_getter=local_custom_getter):
      submodule_a = SimpleModule(name="simple_submodule")
      submodule_b = ComplexModule(name="complex_submodule")
      local_module = ModuleWithSubmodules(
          submodule_a=submodule_a, submodule_b=submodule_b)
    local_module(inputs)  # pylint: disable=not-callable

    self.assertEmpty(local_module.get_all_variables())
    self.assertEmpty(tf.all_variables())
    self.assertLen(tf.local_variables(), 12)

    all_variables = get_non_trainable_variables(local_module)
    all_variable_names = sorted([str(v.name) for v in all_variables])
    self.assertEqual([
        "complex_submodule/linear_1/b:0",
        "complex_submodule/linear_1/w:0",
        "complex_submodule/linear_2/b:0",
        "complex_submodule/linear_2/w:0",
        "module_with_submodules/complex_build/linear_1/b:0",
        "module_with_submodules/complex_build/linear_1/w:0",
        "module_with_submodules/complex_build/linear_2/b:0",
        "module_with_submodules/complex_build/linear_2/w:0",
        "module_with_submodules/simple_build/b:0",
        "module_with_submodules/simple_build/w:0",
        "simple_submodule/b:0",
        "simple_submodule/w:0",
    ], all_variable_names)

  def testGetAllVariablesWithConditionalConstruction(self):
    inputs = tf.ones(dtype=tf.float32, shape=[10, 10])
    cond = tf.constant(0.)
    module_a = SimpleModule(name="module_a")
    module_b = SimpleModule(name="module_b")

    _ = tf.cond(cond > 0, lambda: module_a(inputs), lambda: module_b(inputs))  # pylint: disable=not-callable

    if tf.executing_eagerly():
      # In eager mode only the true branch is taken.
      msg = "module_a not instantiated yet"
      with self.assertRaisesRegexp(base.NotConnectedError, msg):
        module_a.get_all_variables()
    else:
      # check module_a
      all_variables = module_a.get_all_variables()
      all_variable_names = sorted([str(v.name) for v in all_variables])
      self.assertEqual(["module_a/b:0", "module_a/w:0"], all_variable_names)

    # check module_b
    all_variables = module_b.get_all_variables()
    all_variable_names = sorted([str(v.name) for v in all_variables])
    self.assertEqual(["module_b/b:0", "module_b/w:0"], all_variable_names)

  def testCallSignatureAndDocstring(self):
    my_module = SimpleModule()
    self.assertEqual(
        inspect.getargspec(my_module.__call__),
        inspect.getargspec(my_module._build))
    self.assertEqual(my_module.__call__.__doc__, my_module._build.__doc__)


def _make_model_with_params(inputs, output_size):
  weight_shape = [inputs.get_shape().as_list()[-1], output_size]
  weight = tf.get_variable("w", shape=weight_shape, dtype=inputs.dtype)
  return tf.matmul(inputs, weight)


# @tf.contrib.eager.run_all_tests_in_graph_and_eager_modes
class ModuleTest(tf.test.TestCase):

  def testFunctionType(self):
    with self.assertRaises(TypeError) as cm:
      base.Module(build="not_a_function")

    self.assertEqual(str(cm.exception), "Input 'build' must be callable.")

  def testSharing(self):
    batch_size = 3
    in_size = 4
    input_data = np.random.rand(batch_size, in_size)
    inputs1 = tf.constant(input_data)
    inputs2 = tf.constant(input_data)

    build = functools.partial(_make_model_with_params, output_size=10)
    model = base.Module(build)
    self.assertEqual(model.scope_name, "make_model_with_params")
    outputs1 = model(inputs1)
    outputs2 = model(inputs2)

    self.evaluate(tf.global_variables_initializer())
    outputs1, outputs2 = self.evaluate([outputs1, outputs2])
    self.assertAllClose(outputs1, outputs2)

  def testCustomGetter(self):
    def simple_module_build(inputs):
      w = tf.get_variable("w", dtype=tf.float32, shape=[10, 10])
      b = tf.get_variable("b", dtype=tf.float32, shape=[10, 10])
      return w * inputs + b

    connection_count = {"x": 0}

    def custom_getter(getter, name, *args, **kwargs):
      connection_count["x"] += 1
      return getter(name, *args, **kwargs)

    create_module = functools.partial(base.Module, build=simple_module_build)

    inputs = tf.ones(dtype=tf.float32, shape=[10, 10])

    with tf.variable_scope("scope"):
      module = create_module(name="mod1")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(0, connection_count["x"])

      module = create_module(custom_getter=custom_getter, name="mod2")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(2, connection_count["x"])  # w & b

      module = create_module(custom_getter={"w": custom_getter}, name="mod3")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(3, connection_count["x"])  # w

      module = create_module(custom_getter={"w.*": custom_getter}, name="mod3")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(4, connection_count["x"])  # w

      module = create_module(custom_getter={".*": custom_getter}, name="mod4")
      module(inputs)  # pylint: disable=not-callable
      self.assertEqual(6, connection_count["x"])  # w & b

      err = r"More than one custom_getter matched scope/mod5/w \(w\):.*"
      with self.assertRaisesRegexp(KeyError, err):
        module = create_module(
            custom_getter={".*": custom_getter, "w.*": custom_getter},
            name="mod5")
        module(inputs)  # pylint: disable=not-callable

      err = "Given custom_getter is not callable."
      with self.assertRaisesRegexp(TypeError, err):
        module = create_module(custom_getter=0, name="mod6")
      with self.assertRaisesRegexp(TypeError, err):
        module = create_module(custom_getter={"w": 0}, name="mod7")

  def testGetVariablesDifferentGraphScope(self):
    with tf.Graph().as_default():
      inputs = tf.constant(np.random.rand(10, 10), dtype=tf.float32)
      simple_module = SimpleModule()
      simple_module(inputs)  # pylint: disable=not-callable
      # Should have 2 variables whether queried in or out of the Graph scope.
      self.assertEqual(len(simple_module.get_variables()), 2)
    self.assertEqual(len(simple_module.get_variables()), 2)

  def testGraphProperty(self):
    with tf.Graph().as_default() as graph_1:
      id_a = IdentityModule()
      id_a(tf.constant(np.zeros(10)))  # pylint: disable=not-callable
      id_b = IdentityModule()
      id_b(tf.constant(np.ones(5)))  # pylint: disable=not-callable
    with tf.Graph().as_default() as graph_2:
      id_c = IdentityModule()
      id_c(tf.constant(np.eye(3)))  # pylint: disable=not-callable
    self.assertEqual(id_a.graph, id_b.graph)
    self.assertEqual(id_a.graph, graph_1)
    self.assertNotEqual(id_a.graph, id_c.graph)
    self.assertEqual(id_c.graph, graph_2)


class ConnectionObserverTest(tf.test.TestCase):

  def _connection_observer(self, subgraph):
    self._connected_subgraphs.append(subgraph)

  def setUp(self):
    self._inputs = tf.zeros(shape=(10, 10), dtype=tf.float32)
    self._connected_subgraphs = []

  def testObservesWrappedFunction(self):
    activation_module = base.Module(tf.nn.relu)
    with base.observe_connections(self._connection_observer):
      outputs = activation_module(self._inputs)

    self.assertEqual(1, len(self._connected_subgraphs))

    self.assertIs(activation_module, self._connected_subgraphs[0].module)
    self.assertIs(self._inputs, self._connected_subgraphs[0].inputs["args"][0])
    self.assertIs(self._connected_subgraphs[0].outputs, outputs)

  def testObservesSimpleModule(self):
    simple_module = SimpleModule()
    with base.observe_connections(self._connection_observer):
      outputs = simple_module(self._inputs)

    self.assertEqual(1, len(self._connected_subgraphs))

    self.assertIs(simple_module, self._connected_subgraphs[0].module)
    self.assertIs(self._inputs, self._connected_subgraphs[0].inputs["inputs"])
    self.assertIs(self._connected_subgraphs[0].outputs, outputs)

  def testObservesComplexModule(self):
    complex_module = ComplexModule()
    with base.observe_connections(self._connection_observer):
      outputs = complex_module(self._inputs)

    self.assertEqual(3, len(self._connected_subgraphs))

    self.assertIsInstance(self._connected_subgraphs[0].module, SimpleModule)
    self.assertIs(self._inputs, self._connected_subgraphs[0].inputs["inputs"])

    self.assertIsInstance(self._connected_subgraphs[1].module, SimpleModule)
    self.assertIs(self._connected_subgraphs[0].outputs,
                  self._connected_subgraphs[1].inputs["inputs"])
    self.assertIs(self._connected_subgraphs[1].outputs, outputs)

    self.assertIs(complex_module, self._connected_subgraphs[2].module)
    self.assertIs(self._connected_subgraphs[2].outputs, outputs)


class MatMulModule(base.AbstractModule):

  call_count = 0

  def _build(self, x):
    self.call_count += 1
    w = tf.get_variable("w", [x.shape[1], 32])
    return x * w


class DefunTest(tf.test.TestCase):

  def testDefunWrappedProperty(self):
    module = MatMulModule()
    self.assertFalse(module.defun_wrapped)
    for _ in range(2):
      module.defun()
      self.assertTrue(module.defun_wrapped)

  def testCallWithDefun(self):
    module = MatMulModule()
    module.defun()
    batch_size = 10
    output = module(tf.zeros([batch_size, 1]))
    self.assertListEqual(output.shape.as_list(), [batch_size, 32])

  def testCallWithDefunTracingTwice(self):
    module = MatMulModule()
    module.defun()

    batch_size = 10
    for _ in range(2):
      output = module(tf.zeros([batch_size, 1]))
      self.assertListEqual(output.shape.as_list(), [batch_size, 32])
    self.assertEqual(module.call_count, 1)

    # Calling with a different batch_size causes `defun` to re-trace our module.
    batch_size *= 2
    for _ in range(2):
      output = module(tf.zeros([batch_size, 1]))
      self.assertListEqual(output.shape.as_list(), [batch_size, 32])
    self.assertEqual(module.call_count, 2)

if __name__ == "__main__":
  tf.test.main()
