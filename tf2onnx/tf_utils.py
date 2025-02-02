# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT license.

"""
tf2onnx.tf_utils - misc utilities for tf2onnx that interface with tensorflow
"""

from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import collections
from distutils.version import LooseVersion

import six
import numpy as np
import tensorflow as tf
from tensorflow.core.framework import types_pb2, tensor_pb2
from tensorflow.python.framework import tensor_util
from tensorflow.python.framework import graph_util
from .graph_transforms import TransformGraph

from onnx import helper, onnx_pb, numpy_helper

from tf2onnx.utils import make_sure, is_tf_const_op, port_name, node_name
from . import logging

logger = logging.getLogger(__name__)

#
#  mapping dtypes from tensorflow to onnx
#
TF_TO_ONNX_DTYPE = {
    types_pb2.DT_FLOAT: onnx_pb.TensorProto.FLOAT,
    types_pb2.DT_HALF: onnx_pb.TensorProto.FLOAT16,
    types_pb2.DT_DOUBLE: onnx_pb.TensorProto.DOUBLE,
    types_pb2.DT_INT32: onnx_pb.TensorProto.INT32,
    types_pb2.DT_INT16: onnx_pb.TensorProto.INT16,
    types_pb2.DT_INT8: onnx_pb.TensorProto.INT8,
    types_pb2.DT_UINT8: onnx_pb.TensorProto.UINT8,
    types_pb2.DT_UINT16: onnx_pb.TensorProto.UINT16,
    types_pb2.DT_INT64: onnx_pb.TensorProto.INT64,
    types_pb2.DT_STRING: onnx_pb.TensorProto.STRING,
    types_pb2.DT_COMPLEX64: onnx_pb.TensorProto.COMPLEX64,
    types_pb2.DT_COMPLEX128: onnx_pb.TensorProto.COMPLEX128,
    types_pb2.DT_BOOL: onnx_pb.TensorProto.BOOL,
    types_pb2.DT_RESOURCE: onnx_pb.TensorProto.INT64,  # TODO: hack to allow processing on control flow
    types_pb2.DT_QUINT8: onnx_pb.TensorProto.UINT8,  # TODO: map quint8 to  uint8 for now
}


def tf_to_onnx_tensor(tensor, name=""):
    """Convert tensorflow tensor to onnx tensor."""
    np_data = get_tf_tensor_data(tensor)
    if np_data.dtype == np.object:
        # assume np_data is string, numpy_helper.from_array accepts ndarray,
        # in which each item is of str while the whole dtype is of object.
        try:
            np_data = np_data.astype(np.str).astype(np.object)
        except: # pylint: disable=bare-except
            raise RuntimeError("Not support type: {}".format(type(np_data.flat[0])))
    return numpy_helper.from_array(np_data, name=name)


def get_tf_tensor_data(tensor):
    """Get data from tensor."""
    make_sure(isinstance(tensor, tensor_pb2.TensorProto), "Require TensorProto")
    np_data = tensor_util.MakeNdarray(tensor)
    make_sure(isinstance(np_data, np.ndarray), "{} isn't ndarray".format(np_data))
    return np_data


def get_tf_const_value(op, as_list=True):
    """
    If as_list=True, return the array as a (possibly nested) list.
    Otherwise, return data of type np.ndarray.

    If a tensor is a scalar having value 1,
        when as_list=False, return np.array(1), type is <class 'numpy.ndarray'>
        when as_list=True, return 1, type is <class 'int'>.
    """
    make_sure(is_tf_const_op(op), "{} isn't a const op".format(op.name))
    value = get_tf_tensor_data(op.get_attr("value"))
    if as_list:
        value = value.tolist()
    return value


def get_tf_shape_attr(node):
    """Get shape from tensorflow attr "shape"."""
    dims = None
    try:
        shape = get_tf_node_attr(node, "shape")
        if not shape.unknown_rank:
            dims = [int(d.size) for d in shape.dim]
    except:  # pylint: disable=bare-except
        pass
    return dims


def get_tf_tensor_shape(tensor):
    shape = []
    try:
        shape = tensor.get_shape().as_list()
    except Exception:  # pylint: disable=broad-except
        shape = None
    return shape


def map_tf_dtype(dtype):
    if dtype:
        dtype = TF_TO_ONNX_DTYPE[dtype]
    return dtype


def get_tf_node_attr(node, name):
    """Parser TF node attribute."""
    if six.PY2:
        # For python2, TF get_attr does not accept unicode
        name = str(name)
    return node.get_attr(name)


def get_tf_version():
    return LooseVersion(tf.__version__)


def tf_optimize(inputs, outputs, graph_def, fold_constant=None):
    """Optimize tensorflow graph for inference."""
    transforms = []
    if fold_constant:
        transforms.extend([
            "fold_constants(ignore_errors=true)",
            "remove_attribute(attribute_name=_class)",  # remove node colocation attributes
        ])

    transforms.extend([
        "fold_batch_norms",
        "fold_old_batch_norms",
    ])
    needed_names = [node_name(i) for i in inputs] + [node_name(i) for i in outputs]
    graph_def = graph_util.extract_sub_graph(graph_def, needed_names)
    graph_def = TransformGraph(graph_def, inputs, outputs, transforms)
    return graph_def


def tflist_to_onnx(node_list, shape_override):
    """
    Convert the tf-node list into an onnx graph with minimal rewrites so
    we can use the onnx graph as intermediate graph.
    """

    # ignore the following attributes
    ignored_attr = ["unknown_rank", "_class", "Tshape", "use_cudnn_on_gpu", "Index", "Tpaddings",
                    "TI", "Tparams", "Tindices", "Tlen", "Tdim", "dynamic_size", "Tmultiples",
                    "Tblock_shape", "Tcrops", "index_type", "Taxis", "U", "maxval",
                    "Tout", "Tlabels", "Tindex", "element_shape", "Targmax"]
    # some stats
    op_cnt = collections.Counter()
    attr_cnt = collections.Counter()
    onnx_nodes = []
    output_shapes = {}
    dtypes = {}

    # find outputs
    ops = node_list

    # create dict with output to shape mappings
    for node in ops:
        for out in node.outputs:
            shape = shape_override.get(out.name)
            if shape is None:
                shape = get_tf_tensor_shape(out)
            dtypes[out.name] = map_tf_dtype(out.dtype)
            output_shapes[out.name] = shape

    # minimal conversion of attributes
    for node in ops:
        attr = {}
        takeit = True
        op_cnt[node.type] += 1
        for a in node.node_def.attr:
            attr_cnt[a] += 1
            if a == "dtype":
                attr[a] = map_tf_dtype(get_tf_node_attr(node, "dtype"))
            elif a == "T":
                dtype = get_tf_node_attr(node, "T")
                if dtype:
                    if not isinstance(dtype, list):
                        dtypes[node.name] = map_tf_dtype(dtype)
            elif a in ["output_type", "output_dtype", "out_type", "Tidx", "out_idx"]:
                # Tidx is used by Range
                # out_idx is used by ListDiff
                attr[a] = map_tf_dtype(get_tf_node_attr(node, a))
            elif a == "shape":
                shape = get_tf_shape_attr(node)
                if shape is not None:
                    attr[a] = shape
            elif a == "Tperm":
                pass
            elif a == "value":
                onnx_tensor = tf_to_onnx_tensor(get_tf_node_attr(node, a), name=port_name(node.name))
                attr[a] = onnx_tensor
            elif a == "DstT":
                attr["to"] = map_tf_dtype(get_tf_node_attr(node, "DstT"))
            elif a == "SrcT":
                continue
            elif a in ignored_attr:
                continue
            else:
                attr[a] = get_tf_node_attr(node, a)

        if takeit:
            try:
                input_names = [i.name for i in node.inputs]
                output_names = [i.name for i in node.outputs]
                onnx_node = helper.make_node(node.type, input_names, output_names, name=node.name, **attr)
                onnx_nodes.append(onnx_node)
            except Exception as ex:
                logger.error("pass1 convert failed for %s, ex=%s", node, ex)
                raise

    return onnx_nodes, op_cnt, attr_cnt, output_shapes, dtypes


def tensorflow_to_onnx(graph, shape_override):
    """
    Load tensorflow graph and do a conversion.
    """
    return tflist_to_onnx(graph.get_operations(), shape_override)
