import torch
import torch.nn as nn
import torch.nn.functional as F

from mo.utils.error import Error

# Callback which is executed after nn.Module forward
def forward_hook(self, inputs, output):
    # Skip if we already processed as functional hook
    if isinstance(output, OpenVINOTensor) and output.node_name:
        return output

    graph = inputs[0].graph
    if graph is None:
        raise Error('No graph found')
    layer_type = self.__class__.__name__

    # Create a unique name
    name = graph.unique_id(prefix=layer_type + '_')

    graph.add_node(name, kind='op', op=layer_type, name=name, module=self)

    # Find all inputs
    for idx, inp in enumerate(inputs):
        src_id = inp.node_name
        if src_id is None:
            raise Error('Input not found')

        edge_attrs = {
            'out': 0,
            'in': idx,
            'name': src_id,
            'fw_tensor_debug_info': [(src_id, src_id)],
            'in_attrs': ['in', 'name'],
            'out_attrs': ['out', 'name'],
            'data_attrs': ['fw_tensor_debug_info']
        }
        graph.add_edge(src_id, name, **edge_attrs)

    # state_dict is an OrderedDict that means all the parameterd are
    # ordered by connection
    for idx, (key, value) in enumerate(self.state_dict().items()):
        param_name = name + '/' + key
        graph.add_node(param_name, kind='op', op='Const', value=value.numpy())
        edge_attrs = {
            'out': 0,
            'in': len(inputs) + idx,
            'name': param_name,
            'fw_tensor_debug_info': [(param_name, param_name)],
            'in_attrs': ['in', 'name'],
            'out_attrs': ['out', 'name'],
            'data_attrs': ['fw_tensor_debug_info']
        }
        graph.add_edge(param_name, name, **edge_attrs)


    if not isinstance(output, OpenVINOTensor):
        output = OpenVINOTensor(output)
        output.graph = graph

    output.node_name = name
    return output

# PyTorch functional ops and Tensor operations are not tracked by forward_hook.
# So we need to introduce own tensor type to track them.
HANDLED_FUNCTIONS = {}
class OpenVINOTensor(object):
    def __init__(self, value):
        self._value = value
        self.graph = None
        self.node_name = None
        self.shape = value.shape
        self.requires_grad = self._value.requires_grad
        if self.requires_grad:
            raise Error('Model in training mode is used')

    def __repr__(self):
        return self.node_name

    def tensor(self):
        return self._value

    def numel(self):
        return self._value.numel()

    def dim(self):
        return self._value.dim()

    def data_ptr(self):
        return self._value.data_ptr()

    # Overrides += over tensors
    def __iadd__(self, a):
        self._value += a._value
        class Add(nn.Module):
            pass

        # NOTE: need to recreate OpenVINOTensor to run forward_hook
        output = OpenVINOTensor(self._value)
        output.graph = self.graph
        return forward_hook(Add(), (self, a), output)

    def __add__(self, a):
        if isinstance(a, OpenVINOTensor):
            class Add(nn.Module):
                pass
            res = self._value + a._value
            return forward_hook(Add(), (self, a), res)

        elif isinstance(a, float):
            class Add(nn.Module):
                def __init__(self, value):
                    super().__init__()
                    self.register_buffer('add', torch.tensor(value))

            res = self._value + a
            return forward_hook(Add(a), (self,), res)

    def __getitem__(self, key):
        begin_id = []
        end_id = []
        begin_mask = []
        end_mask = []
        shrink_axis_mask = []

        for item in key:
            if isinstance(item, int):
                begin_id.append(item)
                end_id.append(item + 1)

                shrink_axis_mask.append(1)
                begin_mask.append(1)
                end_mask.append(1)

            elif isinstance(item, slice):
                begin_id.append(item.start if item.start else 0)
                begin_mask.append(1 if item.start else 0)

                end_id.append(item.stop if item.stop else 0)
                end_mask.append(1 if item.stop else 0)

                if (end_id[-1] - begin_id[-1] != 1):
                    shrink_axis_mask.append(0)
                else:
                    shrink_axis_mask.append(1)

        class StridedSlice(nn.Module):
            def __init__(self, begin, end, begin_mask, end_mask, shrink_mask):
                super().__init__() 
                self.begin_mask = begin_mask
                self.end_mask = end_mask 
                self.shrink_axis_mask = shrink_mask
                self.register_buffer('begin_id', torch.tensor(begin))
                self.register_buffer('end_id', torch.tensor(end))

        res = self._value[key] 
        sslice = StridedSlice(begin_id, end_id, begin_mask, end_mask, shrink_axis_mask)

        return forward_hook(sslice, (self,), res)

    def __rmul__(self, a):
        class Mul(nn.Module):
            def __init__(self, value):
                super().__init__()
                self.register_buffer('mul', value)

        res = self._value * a
        return forward_hook(Mul(a), (self,), res)

    # a - value
    def __mul__(self, a):
        class Mul(nn.Module):
            def __init__(self, value):
                super().__init__()
                self.register_buffer('mul', torch.tensor(value))

        res = self._value * a
        return forward_hook(Mul(a), (self,), res)

    def view(self, *shape):
        res = self._value.view(shape)

        class Reshape(nn.Module):
            def __init__(self, shape):
                super().__init__()
                self.shape = shape

        return forward_hook(Reshape(shape), (self,), res)

    def reshape(self, *shape):
        res = OpenVINOTensor(self._value.reshape(shape))
        res.graph = self.graph

        class Reshape(nn.Module):
            def __init__(self, shape):
                super().__init__()
                self.shape = shape

        forward_hook(Reshape(shape), (self,), res)
        return res

    def permute(self, *order):
        res = OpenVINOTensor(self._value.permute(order))
        res.graph = self.graph

        class Transpose(nn.Module):
            def __init__(self, order):
                super().__init__()
                self.order = order

        forward_hook(Transpose(order), (self,), res)
        return res

    def sigmoid(self):
        res = self._value.sigmoid()

        class Sigmoid(nn.Module):
            def __init__(self):
                super().__init__()

        return forward_hook(Sigmoid(), (self,), res)

    def __torch_function__(self, func, types, args=(), kwargs=None):
        if kwargs is None:
            kwargs = {}
        if func not in HANDLED_FUNCTIONS or not all(
            issubclass(t, (torch.Tensor, OpenVINOTensor))
            for t in types
        ):
            return NotImplemented
        return HANDLED_FUNCTIONS[func](*args, **kwargs)


import functools
def implements(torch_function):
    """Register a torch function override for OpenVINOTensor"""
    @functools.wraps(torch_function)
    def decorator(func):
        HANDLED_FUNCTIONS[torch_function] = func
        return func
    return decorator


def register_functional_hook(func):
    @implements(func)
    def function_hook(input, *args, **kwargs):
        output = OpenVINOTensor(func(input.tensor(), *args, **kwargs))
        output.graph = input.graph
        return output

register_functional_hook(F.adaptive_avg_pool2d)
register_functional_hook(F.linear)
register_functional_hook(F.dropout)
register_functional_hook(F.dropout3d)


@implements(F.max_pool2d)
def function_hook(input, *args, **kwargs):

    class MaxPool2d(nn.Module):
        def __init__(self, kernel_size, stride, padding, dilation, return_indices, ceil_mode):
            super().__init__()
            self.kernel_size = kernel_size
            self.stride = stride
            self.padding = padding
            self.dilation = dilation
            self.return_indices = return_indices
            self.ceil_mode = ceil_mode

    output = F.max_pool2d(input.tensor(), *args, **kwargs)
    return forward_hook(MaxPool2d(*args, **kwargs), (input,), output)


@implements(F.avg_pool2d)
def function_hook(input, *args, **kwargs):
    class AvgPool2d(nn.Module):
        def __init__(self, kernel_size, stride, padding):
            super().__init__()
            self.kernel_size = kernel_size
            self.stride = stride
            self.padding = padding

    output = F.avg_pool2d(input.tensor(), *args, **kwargs)
    return forward_hook(AvgPool2d(*args, **kwargs), (input,), output)


@implements(torch.relu_)
def function_hook(input, *args, **kwargs):

    class ReLU(nn.Module):
        def __init__(self):
            super().__init__()

    output = torch.relu_(input.tensor(), *args, **kwargs)
    return forward_hook(ReLU(*args, **kwargs), (input,), output)


@implements(torch.unsqueeze)
def unsqueeze(input, dim):
    class Unsqueeze(nn.Module):
        def __init__(self, dim):
            super().__init__()
            self.register_buffer('unsqueeze_dims', torch.tensor(dim))

    output = torch.unsqueeze(input._value, dim)
    forward_hook(Unsqueeze(dim), (input,), output)
    return forward_hook(Unsqueeze(dim), (input,), output)


@implements(F.relu)
def function_hook(input, *args, **kwargs):

    class ReLU(nn.Module):
        def __init__(self, inplace):
            super().__init__()

    output = F.relu(input.tensor(), *args, **kwargs)
    return forward_hook(ReLU(*args, **kwargs), (input,), output)


@implements(torch.sigmoid)
def function_hook(input, *args, **kwargs):

    class Sigmoid(nn.Module):
        def __init__(self):
            super().__init__()

    output = torch.sigmoid(input.tensor(), *args, **kwargs)
    return forward_hook(Sigmoid(*args, **kwargs), (input,), output)


@implements(F.leaky_relu)
def function_hook(input, *args, **kwargs):

    class LeakyReLU(nn.Module):
        def __init__(self, negative_slope, inplace):
            super().__init__()
            self.negative_slope = negative_slope

    output = F.leaky_relu(input.tensor(), *args, **kwargs)
    return forward_hook(LeakyReLU(*args, **kwargs), (input,), output)


@implements(F.batch_norm)
def function_hook(input, *args, **kwargs):

    class BatchNorm2d(nn.BatchNorm2d):
        def __init__(self, running_mean, running_var, weight, bias, training, momentum, eps):
            if training:
                raise Error('BatchNorm2d in training mode is not implemented')
            super().__init__(num_features=weight.shape[0],
                             momentum=momentum,
                             eps=eps)
            self.load_state_dict({
                'running_mean': running_mean,
                'running_var': running_var,
                'weight': weight,
                'bias': bias,
            })

    output = F.batch_norm(input.tensor(), *args, **kwargs)
    return forward_hook(BatchNorm2d(*args, **kwargs), (input,), output)


@implements(torch.conv2d)
@implements(torch.conv3d)
def function_hook(input, weight, bias, *args, **kwargs):

    base = nn.Conv2d if input.dim() == 4 else nn.Conv3d

    class Convolution(base):
        def __init__(self, weight, bias, stride, padding, dilation, groups):
            super().__init__(in_channels=input.shape[1],
                             out_channels=weight.shape[0],
                             kernel_size=weight.shape[2:],
                             stride=stride,
                             padding=padding,
                             dilation=dilation,
                             groups=groups,
                             bias=not bias is None)
            params = {'weight': weight}
            if not bias is None:
                params['bias'] = bias
            self.load_state_dict(params)

    if input.dim() == 4:
        output = torch.conv2d(input.tensor(), weight, bias, *args, **kwargs)
    elif input.dim() == 5:
        output = torch.conv3d(input.tensor(), weight, bias, *args, **kwargs)
    return forward_hook(Convolution(weight, bias, *args, **kwargs), (input,), output)


@implements(torch.flatten)
def function_hook(input, *args, **kwargs):

    class Flatten(nn.Module):
        def __init__(self, axis):
            super().__init__()
            self.axis = axis

    output = torch.flatten(input.tensor(), *args, **kwargs)
    return forward_hook(Flatten(*args, **kwargs), (input,), output)


@implements(F.instance_norm)
def function_hook(input, *args, **kwargs):
    class InstanceNorm(nn.Module):
        def __init__(self, running_mean, running_var, weight, bias, use_input_stats, momentum, eps):
            super().__init__()
            self.running_mean = running_mean
            self.running_var = running_var
            self.weight = weight
            self.bias = bias
            self.use_input_stats = use_input_stats
            self.momentum = momentum
            self.eps = eps
            self.dims = input.dim()

    output = F.instance_norm(input.tensor(), *args, **kwargs)
    return forward_hook(InstanceNorm(*args, **kwargs), (input,), output)


@implements(F.interpolate)
def function_hook(input, *args, **kwargs):

    class Upsample(nn.Module):
        def __init__(self, size, scale_factor, mode, align_corners, recompute_scale_factor):
            super().__init__()
            self.size = size
            self.scale_factor = scale_factor
            self.mode = mode
            self.align_corners = align_corners
            self.recompute_scale_factor = recompute_scale_factor

    output = F.interpolate(input.tensor(), *args, **kwargs)
    return forward_hook(Upsample(*args, **kwargs), (input,), output)


# x - value
@implements(torch.rsub)
def function_hook(value, x):
    class Sub(nn.Module):
        def __init__(self, value):
            super().__init__()
            self.register_buffer('sub', value)

    res = x._value - value
    return forward_hook(Sub(value), (x,), res)


# Workaround for a bug https://github.com/pytorch/pytorch/issues/34294
original_cat = torch.cat
def concat(inputs, dim=0):
    class Concat(nn.Module):
        def __init__(self, dim):
            super().__init__()
            self.dim = dim

    if not isinstance(inputs[0], OpenVINOTensor):
        return original_cat(inputs, dim)

    tensors = [inp.tensor() for inp in inputs]
    output = OpenVINOTensor(original_cat(tensors, dim))
    output.graph = inputs[0].graph

    forward_hook(Concat(dim), inputs, output)
    return output

torch.cat = concat
