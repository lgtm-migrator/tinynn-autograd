import copy

import numpy as np
import pyopencl as cl

from core.backend.ops_gpu import cl_ctx, cl_queue, cl_rng, alloc_buffer
from core.backend.ops_gpu import binary_op, matmul_op, unary_op, contiguous_op, reduce_op
from utils.math import prod
from utils.dtype import float32

class GPUArray:
    # https://numpy.org/doc/stable/dev/internals.html#numpy-internals
    def __init__(self, data=None, shape=None, dtype=float32):
        if isinstance(data, cl.Buffer):
            self.buffer = data
            assert shape is not None, "cannot infer shape when initialize using clbuffer"
        else:
            if data is not None:
                data = np.asarray(data, dtype=dtype)
                shape = data.shape
            else:
                assert shape is not None, "cannot infer shape when without data"
            self.buffer = alloc_buffer(shape, dtype, data)
        self.shape, self.dtype = tuple(shape), dtype
        self.strides = tuple(prod(shape[i+1:]) for i in range(len(shape)))
        self.offset = 0  # offset relative to the beginning of the buffer
        self.c_contiguous, self.f_contiguous = True, False
        self.update_contiguousness()
        self.register_ops()

    @staticmethod
    def as_gpu_array(obj):
        if not isinstance(obj, GPUArray):
            obj = GPUArray(obj)
        return obj

    def __repr__(self):
        return (f"<GPUArray dtype={self.dtype} shape={self.shape} strides={self.strides} size={self.size} contiguous=({int(self.c_contiguous)}, {int(self.f_contiguous)})>")

    def register_ops(self):
        cls = self.__class__
        for op in ("add", "sub", "mul", "truediv", "pow"):
            setattr(cls, f"__{op}__",
                    (lambda op: lambda a, b: binary_op(op, a, self.as_gpu_array(b)))(op))
            setattr(cls, f"__i{op}__",
                    (lambda op: lambda a, b: binary_op(op, a, self.as_gpu_array(b), ret=a))(op))
            setattr(cls, f"__r{op}__",
                    (lambda op: lambda a, b: binary_op(op, self.as_gpu_array(b), a))(op))
        for op in ("eq", "ge", "gt"):
            setattr(cls, f"__{op}__",
                    (lambda op: lambda a, b: binary_op(op, a, self.as_gpu_array(b)))(op))
        setattr(cls, f"__matmul__", lambda a, b: matmul_op(a, self.as_gpu_array(b)))
        setattr(cls, f"__neg__", lambda a: unary_op("neg", a))

    def __getitem__(self, key):
        # TODO: handle step
        is_basic = lambda k: isinstance(k, (slice, int))
        assert is_basic(key) or all(is_basic(k) for k in key), \
                f"Advantage indexing not supported yet. {key}"
        key = (key,) if is_basic(key) else key
        inst = copy.copy(self)
        reduce = []
        shape = list(inst.shape)
        for i, k in enumerate(key):
            if isinstance(k, int):  # indexing
                if k < 0: k += inst.shape[i]
                assert 0 <= k < inst.shape[i], f"Invalid indexing {key[i]} for tensor {inst.shape}"
                inst.offset += inst.strides[i] * k
                reduce.append(i)
            if isinstance(k, slice):  # slicing
                start = 0 if k.start is None else k.start
                if start < 0: start += inst.shape[i]
                stop = inst.shape[i] if k.stop is None else k.stop
                if stop < 0: stop += inst.shape[i]
                assert 0 <= start < stop <= inst.shape[i], f"Invalid slicing {key[i]} for tensor {inst.shape}"
                shape[i] = stop - start
                inst.offset += inst.strides[i] * start
                inst.c_contiguous, inst.f_contiguous = False, False  # TODO: is still contiguous under certain conditions
        inst.shape = tuple(s for i, s in enumerate(shape) if i not in reduce)
        inst.strides = tuple(s for i, s in enumerate(inst.strides) if i not in reduce)
        return inst

    def __setitem__(self, key, value):
        item = self[key]
        # unary_op("noop", value, ret=item)
        assert False, "TODO: implement assign ops"

    @property
    def size(self):
        return self.buffer.size

    @property
    def ndim(self):
        return len(self.shape)

    @classmethod
    def empty(cls, shape, dtype=float32):
        return cls(shape=shape, dtype=dtype)

    @classmethod
    def zeros(cls, shape, dtype=float32):
        return cls(shape=shape, dtype=dtype).fill(0)

    @classmethod
    def ones(cls, shape, dtype=float32):
        return cls(shape=shape, dtype=dtype).fill(1)

    @classmethod
    def full(cls, shape, value, dtype=float32):
        return cls(shape=shape, dtype=dtype).fill(value)

    @classmethod
    def from_numpy(cls, arr):
        return cls(data=arr)

    @classmethod
    def uniform(cls, a, b, shape, dtype=float32):
        buffer = cl_rng.uniform(a=a, b=b, shape=shape, dtype=dtype, cq=cl_queue).data
        return cls(data=buffer, shape=shape, dtype=dtype)

    @classmethod
    def normal(cls, loc, scale, shape, dtype=float32):
        buffer = cl_rng.normal(mu=loc, sigma=scale, shape=shape, dtype=dtype, cq=cl_queue).data
        return cls(data=buffer, shape=shape, dtype=dtype)

    def numpy(self):
        data = np.empty(self.shape, dtype=self.dtype)
        cl.enqueue_copy(cl_queue, data, self.contiguous().buffer, is_blocking=True)
        return data

    def contiguous(self):
        return contiguous_op(self)

    def reshape(self, shape):
        if -1 in shape:
            size = prod(self.shape)
            assert shape.count(-1) <= 1, "Only one dimension can be inferred"
            axis = shape.index(-1)
            infer = prod([s for s in shape if s != -1])
            assert size % infer == 0, f"Shape {shape} invalid for size {size}"
            shape = (*shape[:axis], size // infer, *shape[axis+1:])

        assert prod(shape) == prod(self.shape), f"Can not reshape {self.shape} to {shape}"
        if self.c_contiguous or self.f_contiguous:
            inst = copy.copy(self)
            if self.c_contiguous:
                strides = (prod(shape[i+1:]) for i in range(len(shape)))
            else:
                strides = (prod(shape[:i]) for i in range(len(shape)))
            inst.shape, inst.strides = tuple(shape), tuple(strides)
            inst.update_contiguousness()
        else:
            inst = self.contiguous().reshape(shape)
        return inst

    def expand(self, shape):
        inst = copy.copy(self)
        assert len(shape) == inst.ndim
        strides = []
        for i, (s1, s2) in enumerate(zip(inst.shape, shape)):
            if s1 < s2:
                assert s1 == 1
            strides.append(0 if s1 < s2 else inst.strides[i])
        inst.shape, inst.strides = tuple(shape), tuple(strides)
        inst.c_contiguous, inst.f_contiguous = False, False
        return inst

    def squeeze(self, axis=None):
        if axis is None:
            axis = [i for i, s in enumerate(self.shape) if s == 1]
        elif isinstance(axis, int):
            axis = [axis]
        assert isinstance(axis, (list, tuple))
        axis = [a if a != -1 else self.ndim - 1 for a in axis]
        shape = [s for i, s in enumerate(self.shape) if i not in axis or self.shape[i] != 1]
        if shape == self.shape:
            return self
        return self.reshape(shape)

    def storage(self):
        data = np.empty((self.buffer.size // self.dtype().itemsize,), dtype=self.dtype)
        cl.enqueue_copy(cl_queue, data, self.buffer, is_blocking=True)
        return data

    def fill(self, value):
        cl.enqueue_fill_buffer(cl_queue, self.buffer, self.dtype(value), 0, self.size)
        return self

    def permute(self, axes):
        inst = copy.copy(self)
        inst.strides = tuple(inst.strides[a] for a in axes)
        inst.shape = tuple(inst.shape[a] for a in axes)
        inst.update_contiguousness()
        return inst

    def update_contiguousness(self):
        strides = [self.strides[i] for i in range(self.ndim) if self.shape[i] != 1]
        sorted_strides = sorted(strides)
        self.f_contiguous = sorted_strides == strides
        self.c_contiguous = sorted_strides[::-1] == strides

    @property
    def T(self):
        axes = tuple(range(self.ndim)[::-1])
        return self.permute(axes=axes)

    def sum(self, axis=None, keepdims=False):
        arr = self.contiguous() if not self.c_contiguous else self
        return reduce_op("sum", arr, axis=axis, keepdims=keepdims)

    def max(self, axis=None, keepdims=False):
        arr = self.contiguous() if not self.c_contiguous else self
        return reduce_op("max", arr, axis=axis, keepdims=keepdims)

    def relu(self, inplace=False):
        return unary_op("relu", self, ret=self if inplace else None)

    def exp(self):
        return unary_op("exp", self)

    def log(self):
        return unary_op("log", self)

    def drelu(self, other):
        return binary_op("drelu", self, self.as_gpu_array(other))

