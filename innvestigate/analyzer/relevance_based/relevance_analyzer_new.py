# Get Python six functionality:
from __future__ import\
    absolute_import, print_function, division, unicode_literals
from builtins import zip
import six
import warnings
warnings.filterwarnings("default", category=DeprecationWarning)

###############################################################################
###############################################################################
###############################################################################

import inspect
import tensorflow as tf
import tensorflow.keras as keras
import tensorflow.keras.backend as K
import tensorflow.keras.models as keras_models
import tensorflow.keras.layers as keras_layers


import numpy as np
from .. import new_base as base
from .. import reverse_map
from innvestigate import layers as ilayers
from innvestigate import utils as iutils
import innvestigate.utils.keras as kutils
from innvestigate.utils.keras import checks as kchecks
from innvestigate.utils.keras import graph as kgraph
from . import relevance_rule_base as rrule
from . import utils as rutils


__all__ = [
    "LRP",
    "LRP_RULES",

    "LRPZ",
    "LRPZIgnoreBias",

    "LRPEpsilon",
    "LRPEpsilonIgnoreBias",

    "LRPWSquare",
    "LRPFlat",

    "LRPAlphaBeta",

    "LRPAlpha2Beta1",
    "LRPAlpha2Beta1IgnoreBias",
    "LRPAlpha1Beta0",
    "LRPAlpha1Beta0IgnoreBias",
    "LRPZPlus",
    "LRPZPlusFast",

    "LRPSequentialPresetA",
    "LRPSequentialPresetB",

    "LRPSequentialPresetAFlat",
    "LRPSequentialPresetBFlat",

    "LRPSequentialCompositeA",
    "LRPSequentialCompositeB",

    "LRPSequentialCompositeAFlat",
    "LRPSequentialCompositeBFlat",
]


###############################################################################
###############################################################################
###############################################################################

# Utility list enabling name mappings via string
LRP_RULES = {
    "Z": rrule.ZRule,
    "ZIgnoreBias": rrule.ZIgnoreBiasRule,

    "Epsilon": rrule.EpsilonRule,
    "EpsilonIgnoreBias": rrule.EpsilonIgnoreBiasRule,

    "WSquare": rrule.WSquareRule,
    "Flat": rrule.FlatRule,

    "AlphaBeta": rrule.AlphaBetaRule,
    "AlphaBetaIgnoreBias": rrule.AlphaBetaIgnoreBiasRule,

    "Alpha2Beta1": rrule.Alpha2Beta1Rule,
    "Alpha2Beta1IgnoreBias": rrule.Alpha2Beta1IgnoreBiasRule,
    "Alpha1Beta0": rrule.Alpha1Beta0Rule,
    "Alpha1Beta0IgnoreBias": rrule.Alpha1Beta0IgnoreBiasRule,

    "ZPlus": rrule.ZPlusRule,
    "ZPlusFast": rrule.ZPlusFastRule,
    "Bounded": rrule.BoundedRule,
}

class BatchNormalizationReverseLayer(kgraph.ReverseMappingBase):
    """Special BN handler that applies the Z-Rule"""

    def __init__(self, layer, *args, **kwargs):
        config = layer.get_config()

        self._center = config['center']
        self._scale = config['scale']
        self._axis = config['axis']

        self._mean = layer.moving_mean
        self._std = layer.moving_variance
        if self._center:
            self._beta = layer.beta
        super(BatchNormalizationReverseLayer, self).__init__(layer, *args, **kwargs)

    def wrap_hook(self, ins, neuron_selection):
        outs = self.layer_func(ins)

        # check if final layer (i.e., no next layers)
        if len(self.layer_next) == 0:
            outs = self._neuron_select(outs, neuron_selection)

        return outs

    def explain_hook(self, ins, reversed_outs, args):
        outs = args
        input_shape = self.input_shape

        # prepare broadcasting shape for layer parameters
        broadcast_shape = [1] * len(input_shape)
        broadcast_shape[self._axis] = input_shape[self._axis]
        broadcast_shape[0] = -1

        # reweight relevances as
        #        x * (y - beta)     R
        # Rin = ---------------- * ----
        #           x - mu          y
        # batch norm can be considered as 3 distinct layers of subtraction,
        # multiplication and then addition. The multiplicative scaling layer
        # has no effect on LRP and functions as a linear activation layer

        minus_mu = keras_layers.Lambda(lambda x: x - K.reshape(self._mean, broadcast_shape))
        minus_beta = keras_layers.Lambda(lambda x: x - K.reshape(self._beta, broadcast_shape))
        prepare_div = keras_layers.Lambda(
            lambda x: x + (K.cast(K.greater_equal(x, 0), K.floatx()) * 2 - 1) * K.epsilon())

        x_minus_mu = minus_mu(ins)
        if self._center:
            y_minus_beta = minus_beta(outs)
        else:
            y_minus_beta = outs

        numerator = [keras_layers.Multiply()([x, ymb, r])
                     for x, ymb, r in zip(ins, y_minus_beta, reversed_outs)]
        denominator = [keras_layers.Multiply()([xmm, y])
                       for xmm, y in zip(x_minus_mu, outs)]

        return [ilayers.SafeDivide()([n, prepare_div(d)])
                for n, d in zip(numerator, denominator)]

class AddReverseLayer(kgraph.ReverseMappingBase):
    """Special Add layer handler that applies the Z-Rule"""

    def __init__(self, layer, *args, **kwargs):
        self._layer_wo_act = kgraph.copy_layer_wo_activation(layer,
                                                             name_template="no_act_%s")
        super(AddReverseLayer, self).__init__(layer, *args, **kwargs)

    def wrap_hook(self, ins, neuron_selection):
        with tf.GradientTape(persistent=True) as tape:
            tape.watch(ins)
            outs = self.layer_func(ins)
            Zs = self._layer_wo_act(ins)

            # check if final layer (i.e., no next layers)
            if len(self.layer_next) == 0:
                outs = self._neuron_select(outs, neuron_selection)
                Zs = self._neuron_select(Zs, neuron_selection)

        return outs, Zs, tape

    def explain_hook(self, ins, reversed_outs, args):
        # the outputs of the pooling operation at each location is the sum of its inputs.
        # the forward message must be known in this case, and are the inputs for each pooling thing.
        # the gradient is 1 for each output-to-input connection, which corresponds to the "weights"
        # of the layer. It should thus be sufficient to reweight the relevances and and do a gradient_wrt
        outs, Zs, tape = args
        # last layer
        if reversed_outs is None:
            reversed_outs = Zs

        # Divide incoming relevance by the activations.
        if len(self.layer_next) > 1:
            tmp = [ilayers.SafeDivide()([r, Zs]) for r in reversed_outs]
        else:
            tmp = ilayers.SafeDivide()([reversed_outs, Zs])
        # Propagate the relevance to input neurons
        # using the gradient.

        #print(self.name, np.shape(reversed_outs), np.shape(ins), np.shape(Zs), np.shape(tmp), type(ins))
        if len(self.input_shape) > 1:
            raise ValueError("Conv Layers should only have one input!")
        if len(self.layer_next) > 1:
            tmp2 = [tape.gradient(Zs, ins, output_gradients=t) for t in tmp]
            # TODO (for all rules) is it correct to add relevances here? should be due to sum conservation?
            ret = keras_layers.Add()([keras_layers.Multiply()([ins, t]) for t in tmp2])
        else:
            tmp2 = tape.gradient(Zs, ins, output_gradients=tmp)
            ret = keras_layers.Multiply()([ins, tmp2])
        return ret

class AveragePoolingReverseLayer(kgraph.ReverseMappingBase):
    """Special AveragePooling handler that applies the Z-Rule"""

    def __init__(self, *args, **kwargs):
        layer = kwargs.pop("layer", None)
        self._layer_wo_act = kgraph.copy_layer_wo_activation(layer,
                                                             name_template="no_act_%s")
        super(AveragePoolingReverseLayer, self).__init__(layer, *args, **kwargs)

    def wrap_hook(self, ins, neuron_selection):
        with tf.GradientTape(persistent=True) as tape:
            tape.watch(ins)
            outs = self.layer_func(ins)
            Zs = self._layer_wo_act(ins)

            # check if final layer (i.e., no next layers)
            if len(self.layer_next) == 0:
                outs = self._neuron_select(outs, neuron_selection)
                Zs = self._neuron_select(Zs, neuron_selection)

        return outs, Zs, tape

    def explain_hook(self, ins, reversed_outs, args):
        # the outputs of the pooling operation at each location is the sum of its inputs.
        # the forward message must be known in this case, and are the inputs for each pooling thing.
        # the gradient is 1 for each output-to-input connection, which corresponds to the "weights"
        # of the layer. It should thus be sufficient to reweight the relevances and and do a gradient_wrt
        outs, Zs, tape = args
        # last layer
        if reversed_outs is None:
            reversed_outs = Zs

        # Divide incoming relevance by the activations.
        if len(self.layer_next) > 1:
            tmp = [ilayers.SafeDivide()([r, Zs]) for r in reversed_outs]
        else:
            tmp = ilayers.SafeDivide()([reversed_outs, Zs])
        # Propagate the relevance to input neurons
        # using the gradient.

        #print(self.name, np.shape(reversed_outs), np.shape(ins), np.shape(Zs), np.shape(tmp), type(ins))
        if len(self.input_shape) > 1:
            raise ValueError("Conv Layers should only have one input!")
        if len(self.layer_next) > 1:
            tmp2 = [tape.gradient(Zs, ins, output_gradients=t) for t in tmp]
            # TODO (for all rules) is it correct to add relevances here? should be due to sum conservation?
            ret = keras_layers.Add()([keras_layers.Multiply()([ins, t]) for t in tmp2])
        else:
            tmp2 = tape.gradient(Zs, ins, output_gradients=tmp)
            ret = keras_layers.Multiply()([ins, tmp2])
        return ret


class LRP(base.ReverseAnalyzerBase):
    """
    Base class for LRP-based model analyzers


    :param model: A Keras model.

    :param rule: A rule can be a  string or a Rule object, lists thereof or a list of conditions [(Condition, Rule), ... ]
      gradient.

    :param input_layer_rule: either a Rule object, atuple of (low, high) the min/max pixel values of the inputs
    :param bn_layer_rule: either a Rule object or None.
      None means dedicated BN rule will be applied.
    """

    def __init__(self, model, *args, **kwargs):
        rule = kwargs.pop("rule", None)
        input_layer_rule = kwargs.pop("input_layer_rule", None)
        bn_layer_rule = kwargs.pop("bn_layer_rule", None)
        bn_layer_fuse_mode = kwargs.pop("bn_layer_fuse_mode", "one_linear")
        assert bn_layer_fuse_mode in ["one_linear", "two_linear"]

        self._add_model_softmax_check()
        self._add_model_check(
            lambda layer: not kchecks.is_convnet_layer(layer),
            "LRP is only tested for convolutional neural networks.",
            check_type="warning",
        )

        # check if rule was given explicitly.
        if rule is None:
            raise ValueError("Need LRP rule(s).")

        if isinstance(rule, list):
            # copy refrences
            self._rule = list(rule)
        else:
            self._rule = rule
        self._input_layer_rule = input_layer_rule
        self._bn_layer_rule = bn_layer_rule
        self._bn_layer_fuse_mode = bn_layer_fuse_mode

        if(
           isinstance(rule, six.string_types) or
           (inspect.isclass(rule) and issubclass(rule, reverse_map.ReplacementLayer)) # NOTE: All LRP rules inherit from reverse_map.ReplacementLayer
        ):
            # the given rule is a single string or single rule implementing cla ss
            use_conditions = True
            rules = [(lambda a: True, rule)]

        elif not isinstance(rule[0], tuple):
            # rule list of rule strings or classes
            use_conditions = False
            rules = list(rule)
        else:
            # rule is list of conditioned rules
            use_conditions = True
            rules = rule


        # create a BoundedRule for input layer handling from given tuple
        if self._input_layer_rule is not None:
            input_layer_rule = self._input_layer_rule
            if isinstance(input_layer_rule, tuple):
                low, high = input_layer_rule

                class BoundedProxyRule(rrule.BoundedRule):
                    def __init__(self, *args, **kwargs):
                        super(BoundedProxyRule, self).__init__(
                            *args, low=low, high=high, **kwargs)
                input_layer_rule = BoundedProxyRule


            if use_conditions is True:
                rules.insert(0,
                             (lambda layer: kchecks.is_input_layer(layer),
                              input_layer_rule))

            else:
                rules.insert(0, input_layer_rule)

        self._rules_use_conditions = use_conditions
        self._rules = rules

        # FINALIZED constructor.
        super(LRP, self).__init__(model, *args, **kwargs)

    def create_rule_mapping(self, layer):
        ##print("in select_rule:", layer.__class__.__name__ , end='->') #debug
        rule_class = None
        if self._rules_use_conditions is True:
            for condition, rule in self._rules:
                if condition(layer):
                    ##print(str(rule)) #debug
                    rule_class = rule
                    break
        else:
            ##print(str(rules[0]), '(via pop)') #debug
            rule_class = self._rules.pop()

        if rule_class is None:
            raise Exception("No rule applies to layer: %s" % layer)

        if isinstance(rule_class, six.string_types):
            rule_class = LRP_RULES[rule_class]
        rule = rule_class

        return rule

    def _create_analysis(self, *args, **kwargs):
        ####################################################################
        ### Functionality responsible for backwards rule selection below ####
        ####################################################################

        # default backward hook
        self._add_conditional_reverse_mapping(
            kchecks.contains_kernel,
            self.create_rule_mapping,
            name="lrp_layer_with_kernel_mapping",
        )

        #specialized backward hooks. TODO: add ReverseLayer class handling layers Without kernel: Add and AvgPool
        bn_layer_rule = self._bn_layer_rule

        if bn_layer_rule is None:
            # todo(alber): get rid of this option!
            # alternatively a default rule should be applied.
            bn_mapping = BatchNormalizationReverseLayer
        else:
            if isinstance(bn_layer_rule, six.string_types):
                bn_layer_rule = LRP_RULES[bn_layer_rule]

            #TODO: this still correct?
            bn_mapping = kgraph.apply_mapping_to_fused_bn_layer(
                bn_layer_rule,
                fuse_mode=self._bn_layer_fuse_mode,
            )
        self._add_conditional_reverse_mapping(
            kchecks.is_batch_normalization_layer,
            bn_mapping,
            name="lrp_batch_norm_mapping",
        )
        self._add_conditional_reverse_mapping(
            kchecks.is_average_pooling,
            AveragePoolingReverseLayer,
            name="lrp_average_pooling_mapping",
        )
        self._add_conditional_reverse_mapping(
            kchecks.is_add_layer,
            AddReverseLayer,
            name="lrp_add_layer_mapping",
        )

        # FINALIZED constructor.
        return super(LRP, self)._create_analysis(*args, **kwargs)

    def _default_reverse_mapping(self, layer):
        ##print("    in _default_reverse_mapping:", reverse_state['layer'].__class__.__name__, '(nid: {})'.format(reverse_state['nid']),  end='->')
        #default_return_layers = [keras_layers.Activation]# TODO extend

        # TODO: test w.r.t. tf2.0
        Xs = layer.input_shape
        Ys = layer.output_shape

        if Xs == Ys:
            # Expect Xs and Ys to have the same shapes.
            # There is not mixing of relevances as there is kernel,
            # therefore we pass them as they are.
            ##print('return R')
            return reverse_map.ReplacementLayer
        else:
            # This branch covers:
            # MaxPooling
            # Max
            # Flatten
            # Reshape
            # Concatenate
            # Cropping
            ##print('ilayers.GradientWRT')
            return self._gradient_reverse_mapping()


###############################################################################
# ANALYZER CLASSES AND COMPOSITES #############################################
###############################################################################

class _LRPFixedParams(LRP):
    pass

class LRPZ(_LRPFixedParams):
    """LRP-analyzer that uses the LRP-Z rule"""
    
    def __init__(self, model, *args, **kwargs):
        super(LRPZ, self).__init__(model, *args,
                                   rule="Z", bn_layer_rule="Z", **kwargs)

class LRPZIgnoreBias(_LRPFixedParams):
    """LRP-analyzer that uses the LRP-Z-ignore-bias rule"""

    def __init__(self, model, *args, **kwargs):
        super(LRPZIgnoreBias, self).__init__(model, *args,
                                             rule="ZIgnoreBias",
                                             bn_layer_rule="ZIgnoreBias",
                                             **kwargs)

class LRPEpsilon(_LRPFixedParams):
    """LRP-analyzer that uses the LRP-Epsilon rule"""

    def __init__(self, model, epsilon=1e-7, bias=True, *args, **kwargs):
        epsilon = rutils.assert_lrp_epsilon_param(epsilon, self)
        self._epsilon = epsilon

        class EpsilonProxyRule(rrule.EpsilonRule):
            """
            Dummy class inheriting from EpsilonRule
            for passing along the chosen parameters from
            the LRP analyzer class to the decopmosition rules.
            """
            def __init__(self, *args, **kwargs):
                super(EpsilonProxyRule, self).__init__(*args,
                                                       epsilon=epsilon,
                                                       bias=bias,
                                                       **kwargs)

        super(LRPEpsilon, self).__init__(model, *args,
                                         rule=EpsilonProxyRule,
                                         bn_layer_rule=EpsilonProxyRule,
                                         **kwargs)

class LRPEpsilonIgnoreBias(LRPEpsilon):
    """LRP-analyzer that uses the LRP-Epsilon-ignore-bias rule"""

    def __init__(self, model, epsilon=1e-7, *args, **kwargs):
        super(LRPEpsilonIgnoreBias, self).__init__(model, *args,
                                                   epsilon=epsilon,
                                                   bias=False,
                                                   **kwargs)

class LRPWSquare(_LRPFixedParams):
    """LRP-analyzer that uses the DeepTaylor W**2 rule"""

    def __init__(self, model, *args, **kwargs):
        super(LRPWSquare, self).__init__(model, *args,
                                         rule="WSquare",
                                         bn_layer_rule="WSquare",
                                         **kwargs)

class LRPFlat(_LRPFixedParams):
    """LRP-analyzer that uses the LRP-Flat rule"""

    def __init__(self, model, *args, **kwargs):
        super(LRPFlat, self).__init__(model, *args,
                                      rule="Flat",
                                      bn_layer_rule="Flat",
                                      **kwargs)

class LRPAlphaBeta(LRP):
    """ Base class for LRP AlphaBeta"""

    def __init__(self, model, alpha=None, beta=None, bias=True, *args, **kwargs):
        alpha, beta = rutils.assert_infer_lrp_alpha_beta_param(alpha, beta, self)
        self._alpha = alpha
        self._beta = beta
        self._bias = bias

        class AlphaBetaProxyRule(rrule.AlphaBetaRule):
            """
            Dummy class inheriting from AlphaBetaRule
            for the purpose of passing along the chosen parameters from
            the LRP analyzer class to the decopmosition rules.
            """
            def __init__(self, *args, **kwargs):
                super(AlphaBetaProxyRule, self).__init__(*args,
                                                         alpha=alpha,
                                                         beta=beta,
                                                         bias=bias,
                                                         **kwargs)

        super(LRPAlphaBeta, self).__init__(model, *args,
                                           rule=AlphaBetaProxyRule,
                                           bn_layer_rule=AlphaBetaProxyRule,
                                           **kwargs)

class _LRPAlphaBetaFixedParams(LRPAlphaBeta):
    pass

class LRPAlpha2Beta1(_LRPAlphaBetaFixedParams):
    """LRP-analyzer that uses the LRP-alpha-beta rule with a=2,b=1"""

    def __init__(self, model, *args, **kwargs):
        super(LRPAlpha2Beta1, self).__init__(model, *args,
                                             alpha=2,
                                             beta=1,
                                             bias=True,
                                             **kwargs)

class LRPAlpha2Beta1IgnoreBias(_LRPAlphaBetaFixedParams):
    """LRP-analyzer that uses the LRP-alpha-beta-ignbias rule with a=2,b=1"""

    def __init__(self, model, *args, **kwargs):
        super(LRPAlpha2Beta1IgnoreBias, self).__init__(model, *args,
                                                       alpha=2,
                                                       beta=1,
                                                       bias=False,
                                                       **kwargs)

class LRPAlpha1Beta0(_LRPAlphaBetaFixedParams):
    """LRP-analyzer that uses the LRP-alpha-beta rule with a=1,b=0"""

    def __init__(self, model, *args, **kwargs):
        super(LRPAlpha1Beta0, self).__init__(model, *args,
                                             alpha=1,
                                             beta=0,
                                             bias=True,
                                             **kwargs)

class LRPAlpha1Beta0IgnoreBias(_LRPAlphaBetaFixedParams):
    """LRP-analyzer that uses the LRP-alpha-beta-ignbias rule with a=1,b=0"""

    def __init__(self, model, *args, **kwargs):
        super(LRPAlpha1Beta0IgnoreBias, self).__init__(model, *args,
                                                       alpha=1,
                                                       beta=0,
                                                       bias=False,
                                                       **kwargs)

class LRPZPlus(LRPAlpha1Beta0IgnoreBias):
    """LRP-analyzer that uses the LRP-alpha-beta rule with a=1,b=0"""
    #TODO: assert that layer inputs are always >= 0
    def __init__(self, model, *args, **kwargs):
        super(LRPZPlus, self).__init__(model, *args, **kwargs)

class LRPZPlusFast(_LRPFixedParams):
    """
    The ZPlus rule is a special case of the AlphaBetaRule
    for alpha=1, beta=0 and assumes inputs x >= 0.
    """
    #TODO: assert that layer inputs are always >= 0
    def __init__(self, model, *args, **kwargs):
        super(LRPZPlusFast, self).__init__(model, *args,
                                           rule="ZPlusFast",
                                           bn_layer_rule="ZPlusFast",
                                           **kwargs)

class LRPSequentialPresetA(_LRPFixedParams): #for the lack of a better name
    """
        Special LRP-configuration for ConvNets
        DEPRECATED: use LRPSequentialCompositeBFlat instead
    """

    def __init__(self, model, epsilon=1e-1, *args, **kwargs):
        warnings.warn("LRPSequentialPresetA is deprecated. Use LRPSequentialCompositeA instead",
                      DeprecationWarning)
        self._add_model_check(
            lambda layer: not kchecks.only_relu_activation(layer),
            #TODO: fix. specify. extend.
            ("LRPSequentialPresetA is not advised "
             "for networks with non-ReLU activations."),
            check_type="warning",
        )

        class EpsilonProxyRule(rrule.EpsilonRule):
            def __init__(self, *args, **kwargs):
                super(EpsilonProxyRule, self).__init__(*args,
                                                       epsilon=epsilon,
                                                       bias=True,
                                                       **kwargs)


        conditional_rules = [(kchecks.is_dense_layer, EpsilonProxyRule),
                             (kchecks.is_conv_layer, rrule.Alpha1Beta0Rule)
                            ]
        bn_layer_rule = kwargs.pop("bn_layer_rule", rrule.AlphaBetaX2m100Rule)

        super(LRPSequentialPresetA, self).__init__(
            model,
            *args,
            rule=conditional_rules,
            bn_layer_rule=bn_layer_rule,
            **kwargs)

class LRPSequentialCompositeA(_LRPFixedParams): #for the lack of a better name
    """Special LRP-configuration for ConvNets"""

    def __init__(self, model, epsilon=1e-1, *args, **kwargs):

        self._add_model_check(
            lambda layer: not kchecks.only_relu_activation(layer),
            #TODO: fix. specify. extend.
            ("LRPSequentialCompositeA is not advised "
             "for networks with non-ReLU activations."),
            check_type="warning",
        )

        class EpsilonProxyRule(rrule.EpsilonRule):
            def __init__(self, *args, **kwargs):
                super(EpsilonProxyRule, self).__init__(*args,
                                                       epsilon=epsilon,
                                                       bias=True,
                                                       **kwargs)


        conditional_rules = [(kchecks.is_dense_layer, EpsilonProxyRule),
                             (kchecks.is_conv_layer, rrule.Alpha1Beta0Rule)
                            ]
        bn_layer_rule = kwargs.pop("bn_layer_rule", rrule.AlphaBetaX2m100Rule)

        super(LRPSequentialCompositeA, self).__init__(
            model,
            *args,
            rule=conditional_rules,
            bn_layer_rule=bn_layer_rule,
            **kwargs)

class LRPSequentialPresetB(_LRPFixedParams):
    """
        Special LRP-configuration for ConvNets
        DEPRECATED: use LRPSequentialCompositeBFlat instead
    """

    def __init__(self, model, epsilon=1e-1, *args, **kwargs):
        warnings.warn("LRPSequentialPresetB is deprecated. Use LRPSequentialCompositeB instead",
                      DeprecationWarning)
        self._add_model_check(
            lambda layer: not kchecks.only_relu_activation(layer),
            #TODO: fix. specify. extend.
            ("LRPSequentialPresetB is not advised "
             "for networks with non-ReLU activations."),
            check_type="warning",
        )

        class EpsilonProxyRule(rrule.EpsilonRule):
            def __init__(self, *args, **kwargs):
                super(EpsilonProxyRule, self).__init__(*args,
                                                       epsilon=epsilon,
                                                       bias=True,
                                                       **kwargs)

        conditional_rules = [(kchecks.is_dense_layer, EpsilonProxyRule),
                             (kchecks.is_conv_layer, rrule.Alpha2Beta1Rule)
                         ]
        bn_layer_rule = kwargs.pop("bn_layer_rule", rrule.AlphaBetaX2m100Rule)

        super(LRPSequentialPresetB, self).__init__(
            model,
            *args,
            rule=conditional_rules,
            bn_layer_rule=bn_layer_rule,
            **kwargs)

class LRPSequentialCompositeB(_LRPFixedParams):
    """Special LRP-configuration for ConvNets"""

    def __init__(self, model, epsilon=1e-1, *args, **kwargs):
        self._add_model_check(
            lambda layer: not kchecks.only_relu_activation(layer),
            #TODO: fix. specify. extend.
            ("LRPSequentialCompositeB is not advised "
             "for networks with non-ReLU activations."),
            check_type="warning",
        )

        class EpsilonProxyRule(rrule.EpsilonRule):
            def __init__(self, *args, **kwargs):
                super(EpsilonProxyRule, self).__init__(*args,
                                                       epsilon=epsilon,
                                                       bias=True,
                                                       **kwargs)

        conditional_rules = [(kchecks.is_dense_layer, EpsilonProxyRule),
                             (kchecks.is_conv_layer, rrule.Alpha2Beta1Rule)
                         ]
        bn_layer_rule = kwargs.pop("bn_layer_rule", rrule.AlphaBetaX2m100Rule)

        super(LRPSequentialCompositeB, self).__init__(
            model,
            *args,
            rule=conditional_rules,
            bn_layer_rule=bn_layer_rule,
            **kwargs)

#TODO: allow to pass input layer identification by index or id.
class LRPSequentialPresetAFlat(LRPSequentialPresetA):
    """
        Special LRP-configuration for ConvNets
        DEPRECATED: use LRPSequentialCompositeBFlat instead
    """

    def __init__(self, model, *args, **kwargs):
        warnings.warn("LRPSequentialPresetAFlat is deprecated. Use LRPSequentialCompositeAFlat instead",
                      DeprecationWarning)
        super(LRPSequentialPresetAFlat, self).__init__(model,
                                                *args,
                                                input_layer_rule="Flat",
                                                **kwargs)

#TODO: allow to pass input layer identification by index or id.
class LRPSequentialCompositeAFlat(LRPSequentialCompositeA):
    """Special LRP-configuration for ConvNets"""

    def __init__(self, model, *args, **kwargs):
        super(LRPSequentialCompositeAFlat, self).__init__(model,
                                                *args,
                                                input_layer_rule="Flat",
                                                **kwargs)


#TODO: allow to pass input layer identification by index or id.
class LRPSequentialPresetBFlat(LRPSequentialPresetB):
    """
        Special LRP-configuration for ConvNets
        DEPRECATED: use LRPSequentialCompositeBFlat instead
    """

    def __init__(self, model, *args, **kwargs):
        warnings.warn("LRPSequentialPresetBFlat is deprecated. Use LRPSequentialCompositeBFlat instead",
                      DeprecationWarning)
        super(LRPSequentialPresetBFlat, self).__init__(model,
                                                *args,
                                                input_layer_rule="Flat",
                                                **kwargs)

#TODO: allow to pass input layer identification by index or id.
class LRPSequentialCompositeBFlat(LRPSequentialCompositeB):
    """Special LRP-configuration for ConvNets"""

    def __init__(self, model, *args, **kwargs):
        super(LRPSequentialCompositeBFlat, self).__init__(model,
                                                *args,
                                                input_layer_rule="Flat",
                                                **kwargs)