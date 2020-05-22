# MIT License
#
# Copyright (C) The Adversarial Robustness Toolbox (ART) Authors 2020
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This module implements the Projected Gradient Descent attack `ProjectedGradientDescent` as an iterative method in which,
after each iteration, the perturbation is projected on an lp-ball of specified radius (in addition to clipping the
values of the adversarial sample so that it lies in the permitted data range). This is the attack proposed by Madry et
al. for adversarial training.

| Paper link: https://arxiv.org/abs/1706.06083
"""
from __future__ import absolute_import, division, print_function, unicode_literals

import logging

import numpy as np
import torch

from art.config import ART_NUMPY_DTYPE
from art.attacks.evasion.projected_gradient_descent.projected_gradient_descent_numpy import (
    ProjectedGradientDescentCommon,
)
from art.utils import compute_success, random_sphere

logger = logging.getLogger(__name__)


class ProjectedGradientDescentPyTorch(ProjectedGradientDescentCommon):
    """
    The Projected Gradient Descent attack is an iterative method in which,
    after each iteration, the perturbation is projected on an lp-ball of specified radius (in
    addition to clipping the values of the adversarial sample so that it lies in the permitted
    data range). This is the attack proposed by Madry et al. for adversarial training.

    | Paper link: https://arxiv.org/abs/1706.06083
    """

    def __init__(
        self,
        estimator,
        norm=np.inf,
        eps=0.3,
        eps_step=0.1,
        max_iter=100,
        targeted=False,
        num_random_init=0,
        batch_size=32,
        random_eps=False,
    ):
        """
        Create a :class:`.ProjectedGradientDescentPytorch` instance.

        :param estimator: An trained estimator.
        :type estimator: :class:`.BaseEstimator`
        :param norm: The norm of the adversarial perturbation. Possible values: np.inf, 1 or 2.
        :type norm: `int`
        :param eps: Maximum perturbation that the attacker can introduce.
        :type eps: `float`
        :param eps_step: Attack step size (input variation) at each iteration.
        :type eps_step: `float`
        :param random_eps: When True, epsilon is drawn randomly from truncated normal distribution. The literature
                           suggests this for FGSM based training to generalize across different epsilons. eps_step
                           is modified to preserve the ratio of eps / eps_step. The effectiveness of this
                           method with PGD is untested (https://arxiv.org/pdf/1611.01236.pdf).
        :type random_eps: `bool`
        :param max_iter: The maximum number of iterations.
        :type max_iter: `int`
        :param targeted: Indicates whether the attack is targeted (True) or untargeted (False)
        :type targeted: `bool`
        :param num_random_init: Number of random initialisations within the epsilon ball. For num_random_init=0
            starting at the original input.
        :type num_random_init: `int`
        :param batch_size: Size of the batch on which adversarial samples are generated.
        :type batch_size: `int`
        """
        if (hasattr(estimator, "preprocessing") and estimator.preprocessing is not None) or (
            hasattr(estimator, "preprocessing_defences") and estimator.preprocessing_defences is not None
        ):
            logging.warning(
                "The framework-specific implementation currently does not apply preprocessing and "
                "preprocessing defences."
            )

        super(ProjectedGradientDescentPyTorch, self).__init__(
            estimator=estimator,
            norm=norm,
            eps=eps,
            eps_step=eps_step,
            max_iter=max_iter,
            targeted=targeted,
            num_random_init=num_random_init,
            batch_size=batch_size,
            random_eps=random_eps,
        )

    def generate(self, x, y=None, **kwargs):
        """
        Generate adversarial samples and return them in an array.

        :param x: An array with the original inputs.
        :type x: `np.ndarray`
        :param y: Target values (class labels) one-hot-encoded of shape `(nb_samples, nb_classes)` or indices of shape
                  (nb_samples,). Only provide this parameter if you'd like to use true labels when crafting adversarial
                  samples. Otherwise, model predictions are used as labels to avoid the "label leaking" effect
                  (explained in this paper: https://arxiv.org/abs/1611.01236). Default is `None`.
        :type y: `np.ndarray`
        :param mask: An array with a mask to be applied to the adversarial perturbations. Shape needs to be
                     broadcastable to the shape of x. Any features for which the mask is zero will not be adversarially
                     perturbed.
        :type mask: `np.ndarray`
        :return: An array holding the adversarial examples.
        :rtype: `np.ndarray`
        """
        # Check whether random eps is enabled
        self._random_eps()

        # Set up targets
        targets = self._set_targets(x, y)

        # Get the mask
        mask = self._get_mask(x, **kwargs)

        # Create dataset
        if mask is not None:
            # Here we need to make a distinction: if the masks are different for each input, we need to index
            # those for the current batch. Otherwise (i.e. mask is meant to be broadcasted), keep it as it is.
            if len(mask.shape) == len(x.shape):
                dataset = torch.utils.data.TensorDataset(
                    torch.from_numpy(x.astype(ART_NUMPY_DTYPE)),
                    torch.from_numpy(targets.astype(ART_NUMPY_DTYPE)),
                    torch.from_numpy(mask.astype(ART_NUMPY_DTYPE)),
                )

            else:
                dataset = torch.utils.data.TensorDataset(
                    torch.from_numpy(x.astype(ART_NUMPY_DTYPE)),
                    torch.from_numpy(targets.astype(ART_NUMPY_DTYPE)),
                    torch.from_numpy(np.array([mask.astype(ART_NUMPY_DTYPE)] * x.shape[0])),
                )

        else:
            dataset = torch.utils.data.TensorDataset(
                torch.from_numpy(x.astype(ART_NUMPY_DTYPE)), torch.from_numpy(targets.astype(ART_NUMPY_DTYPE)),
            )

        data_loader = torch.utils.data.DataLoader(
            dataset=dataset, batch_size=self.batch_size, shuffle=False, drop_last=False
        )

        # Start to compute adversarial examples
        adv_x_best = None
        rate_best = None

        for _ in range(max(1, self.num_random_init)):
            adv_x = x.astype(ART_NUMPY_DTYPE)

            # Compute perturbation with batching
            for (batch_id, batch_all) in enumerate(data_loader):
                if mask is not None:
                    (batch, batch_labels, mask_batch) = batch_all[0], batch_all[1], batch_all[2]
                else:
                    (batch, batch_labels, mask_batch) = batch_all[0], batch_all[1], None

                batch_index_1, batch_index_2 = batch_id * self.batch_size, (batch_id + 1) * self.batch_size
                adv_x[batch_index_1:batch_index_2] = self._generate_batch(batch, batch_labels, mask_batch)

            if self.num_random_init > 1:
                rate = 100 * compute_success(
                    self.estimator, x, targets, adv_x, self.targeted, batch_size=self.batch_size
                )
                if rate_best is None or rate > rate_best or adv_x_best is None:
                    rate_best = rate
                    adv_x_best = adv_x
            else:
                adv_x_best = adv_x

        logger.info(
            "Success rate of attack: %.2f%%",
            rate_best
            if rate_best is not None
            else 100 * compute_success(self.estimator, x, y, adv_x_best, self.targeted, batch_size=self.batch_size),
        )

        return adv_x_best

    def _generate_batch(self, x, targets, mask):
        """
        Generate a batch of adversarial samples and return them in an array.

        :param x: An array with the original inputs.
        :type x: `torch.Tensor`
        :param targets: Target values (class labels) one-hot-encoded of shape `(nb_samples, nb_classes)`.
        :type targets: `torch.Tensor`
        :param mask: An array with a mask to be applied to the adversarial perturbations. Shape needs to be
                     broadcastable to the shape of x. Any features for which the mask is zero will not be adversarially
                     perturbed.
        :type mask: `torch.Tensor`
        :return: Adversarial examples.
        :rtype: `np.ndarray`
        """
        inputs = x.to(self.estimator.device)
        targets = targets.to(self.estimator.device)
        adv_x = inputs

        if mask is not None:
            mask = mask.to(self.estimator.device)

        for i_max_iter in range(self.max_iter):
            adv_x = self._compute(
                adv_x, inputs, targets, mask, self.eps, self.eps_step, self.num_random_init > 0 and i_max_iter == 0,
            )

        return adv_x.cpu().detach().numpy()

    def _compute_perturbation(self, x, y, mask):
        """
        Compute perturbations.

        :param x: Current adversarial examples.
        :type x: `torch.Tensor`
        :param y: Target values (class labels) one-hot-encoded of shape `(nb_samples, nb_classes)` or indices of shape
                  (nb_samples,). Only provide this parameter if you'd like to use true labels when crafting adversarial
                  samples. Otherwise, model predictions are used as labels to avoid the "label leaking" effect
                  (explained in this paper: https://arxiv.org/abs/1611.01236). Default is `None`.
        :type y: `torch.Tensor`
        :param mask: An array with a mask to be applied to the adversarial perturbations. Shape needs to be
                     broadcastable to the shape of x. Any features for which the mask is zero will not be adversarially
                     perturbed.
        :type mask: `torch.Tensor`
        :return: Perturbations.
        :rtype: `torch.Tensor`
        """
        # Pick a small scalar to avoid division by 0
        tol = 10e-8

        # Get gradient wrt loss; invert it if attack is targeted
        grad = self.estimator.loss_gradient_framework(x, y) * (1 - 2 * int(self.targeted))

        # Apply norm bound
        if self.norm == np.inf:
            grad = grad.sign()

        elif self.norm == 1:
            ind = tuple(range(1, len(x.shape)))
            grad = grad / (torch.sum(grad.abs(), dim=ind, keepdims=True) + tol)

        elif self.norm == 2:
            ind = tuple(range(1, len(x.shape)))
            grad = grad / (torch.sqrt(torch.sum(grad * grad, axis=ind, keepdims=True)) + tol)

        assert x.shape == grad.shape

        if mask is None:
            return grad
        else:
            return grad * mask

    def _apply_perturbation(self, x, perturbation, eps_step):
        """
        Apply perturbation on examples.

        :param x: Current adversarial examples.
        :type x: `torch.Tensor`
        :param perturbation: Current perturbations.
        :type perturbation: `torch.Tensor`
        :param eps_step: Attack step size (input variation) at each iteration.
        :type eps_step: `float`
        :return: Adversarial examples.
        :rtype: `torch.Tensor`
        """
        x = x + eps_step * perturbation

        if hasattr(self.estimator, "clip_values") and self.estimator.clip_values is not None:
            clip_min, clip_max = self.estimator.clip_values
            x = torch.clamp(x, clip_min, clip_max)

        return x

    def _compute(self, x, x_init, y, mask, eps, eps_step, random_init):
        """
        Compute adversarial examples for one iteration.

        :param x: Current adversarial examples.
        :type x: `torch.Tensor`
        :param x_init: An array with the original inputs.
        :type x_init: `torch.Tensor`
        :param y: Target values (class labels) one-hot-encoded of shape `(nb_samples, nb_classes)` or indices of shape
                  (nb_samples,). Only provide this parameter if you'd like to use true labels when crafting adversarial
                  samples. Otherwise, model predictions are used as labels to avoid the "label leaking" effect
                  (explained in this paper: https://arxiv.org/abs/1611.01236). Default is `None`.
        :type y: `torch.Tensor`
        :param mask: An array with a mask to be applied to the adversarial perturbations. Shape needs to be
                     broadcastable to the shape of x. Any features for which the mask is zero will not be adversarially
                     perturbed.
        :type mask: `torch.Tensor`
        :param eps: Maximum perturbation that the attacker can introduce.
        :type eps: `float`
        :param eps_step: Attack step size (input variation) at each iteration.
        :type eps_step: `float`
        :param random_init: Random initialisation within the epsilon ball. For random_init=False
            starting at the original input.
        :type random_init: `bool`
        :return: Adversarial examples.
        :rtype: `torch.Tensor`
        """
        if random_init:
            n = x.shape[0]
            m = np.prod(x.shape[1:])

            random_perturbation = random_sphere(n, m, eps, self.norm).reshape(x.shape).astype(ART_NUMPY_DTYPE)
            random_perturbation = torch.from_numpy(random_perturbation).to(self.estimator.device)

            if mask is not None:
                random_perturbation = random_perturbation * mask

            x_adv = x + random_perturbation

            if hasattr(self.estimator, "clip_values") and self.estimator.clip_values is not None:
                clip_min, clip_max = self.estimator.clip_values
                x_adv = torch.clamp(x_adv, clip_min, clip_max)

        else:
            x_adv = x

        # Get perturbation
        perturbation = self._compute_perturbation(x_adv, y, mask)

        # Apply perturbation and clip
        x_adv = self._apply_perturbation(x_adv, perturbation, eps_step)

        # Do projection
        perturbation = self._projection(x_adv - x_init, eps, self.norm)

        # Recompute x_adv
        x_adv = perturbation + x_init

        return x_adv

    def _projection(self, values, eps, norm_p):
        """
        Project `values` on the L_p norm ball of size `eps`.

        :param values: Values to clip.
        :type values: `torch.Tensor`
        :param eps: Maximum norm allowed.
        :type eps: `float`
        :param norm_p: L_p norm to use for clipping. Only 1, 2 and `np.Inf` supported for now.
        :type norm_p: `int`
        :return: Values of `values` after projection.
        :rtype: `torch.Tensor`
        """
        # Pick a small scalar to avoid division by 0
        tol = 10e-8
        values_tmp = values.reshape(values.shape[0], -1)

        if norm_p == 2:
            values_tmp = values_tmp * torch.min(
                torch.FloatTensor([1.0]).to(self.estimator.device), eps / (torch.norm(values_tmp, p=2, dim=1) + tol)
            ).unsqueeze_(-1)

        elif norm_p == 1:
            values_tmp = values_tmp * torch.min(
                torch.FloatTensor([1.0]).to(self.estimator.device), eps / (torch.norm(values_tmp, p=1, dim=1) + tol)
            ).unsqueeze_(-1)

        elif norm_p == np.inf:
            values_tmp = values_tmp.sign() * torch.min(
                values_tmp.abs(), torch.FloatTensor([eps]).to(self.estimator.device)
            )

        else:
            raise NotImplementedError(
                "Values of `norm_p` different from 1, 2 and `np.inf` are currently not supported."
            )

        values = values_tmp.reshape(values.shape)

        return values
