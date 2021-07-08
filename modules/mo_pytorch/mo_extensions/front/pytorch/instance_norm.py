from mo.front.common.partial_infer.elemental import copy_shape_infer
from mo.front.common.replacement import FrontReplacementOp
from mo.graph.graph import Graph, Node
from mo.ops.op import Op
from extensions.ops.mvn import MVN
from extensions.ops.elementwise import Mul, Add
from mo.ops.const import Const
import numpy as np
from .batchnorm import BatchNorm

class InstanceNorm3d(FrontReplacementOp):
    op = 'InstanceNorm'
    enabled = True

    def replace_op(self, graph: Graph, node: Node):
        mean = node.module.running_mean.detach().numpy()
        var = node.module.running_var.detach().numpy()
        weight = node.module.weight.detach().numpy()
        bias = node.module.bias.detach().numpy()

        w = weight / np.sqrt(var + node.module.eps)
        b = bias - w * mean

        w = Const(graph, {'value': w.reshape(1, -1, 1, 1, 1)}).create_node()
        b = Const(graph, {'value': b.reshape(1, -1, 1, 1, 1)}).create_node()
        mul = Mul(graph, dict(name=node.name + '/mul')).create_node([node.in_node(0), w])
        add = Add(graph, dict(name=node.name + '/add')).create_node([mul, b])

        return [add.id]