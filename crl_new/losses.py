from typing import Any, NamedTuple

from brax.training import types
from optax import sigmoid_binary_cross_entropy

from crl_new import networks as crl_networks
from brax.training.types import Params
from brax.training.types import PRNGKey
import jax
import jax.numpy as jnp


Transition = types.Transition


def make_losses(
    config: NamedTuple,
    contrastive_loss_fn: str,
    logsumexp_penalty: float,
    crl_network: crl_networks.CRLNetworks,
    action_size: int,
):
    """Creates the CRL losses."""

    target_entropy = -0.5 * action_size
    policy_network = crl_network.policy_network
    parametric_action_distribution = crl_network.parametric_action_distribution
    sa_encoder = crl_network.sa_encoder
    g_encoder = crl_network.g_encoder
    obs_dim = config.obs_dim

    def alpha_loss(
        log_alpha: jnp.ndarray,
        policy_params: Params,
        normalizer_params: Any,
        transitions: Transition,
        key: PRNGKey,
    ) -> jnp.ndarray:
        """Eq 18 from https://arxiv.org/pdf/1812.05905.pdf."""
        dist_params = policy_network.apply(normalizer_params, policy_params, transitions.observation)
        action = parametric_action_distribution.sample_no_postprocessing(dist_params, key)
        log_prob = parametric_action_distribution.log_prob(dist_params, action)
        alpha = jnp.exp(log_alpha)
        alpha_loss = alpha * jax.lax.stop_gradient(-log_prob - target_entropy)
        return jnp.mean(alpha_loss)

    def crl_critic_loss(
        crl_critic_params: Params,
        normalizer_params: Any,
        transitions: Transition,
    ):

        sa_encoder_params, g_encoder_params = (
            crl_critic_params["sa_encoder"],
            crl_critic_params["g_encoder"],
        )
        sa_repr = sa_encoder.apply(
            normalizer_params,
            sa_encoder_params,
            jnp.concatenate([transitions.observation[:, :obs_dim], transitions.action], axis=-1),
        )
        g_repr = g_encoder.apply(normalizer_params, g_encoder_params, transitions.observation[:, obs_dim:])
        logits = jnp.einsum("ik,jk->ij", sa_repr, g_repr)

        if contrastive_loss_fn == "binary":
            loss = jnp.mean(
                sigmoid_binary_cross_entropy(logits, labels=jnp.eye(logits.shape[0]))
            )  # shape[0] - is a batch size
        elif contrastive_loss_fn == "symmetric_infonce":
            logits1 = jax.nn.log_softmax(logits, axis=1)
            logits2 = jax.nn.log_softmax(logits, axis=0)
            loss = -jnp.mean(jnp.diag(logits1) + jnp.diag(logits2))
        elif contrastive_loss_fn == "infonce":
            logits1 = jax.nn.log_softmax(logits, axis=1)
            I = jnp.eye(logits1.shape[0])
            loss = -jnp.mean(jnp.diag(logits1))
        elif contrastive_loss_fn == "infonce_backward":
            logits2 = jax.nn.log_softmax(logits, axis=0)
            I = jnp.eye(logits2.shape[0])
            loss = -jnp.mean(jnp.diag(logits2))
        else:
            raise ValueError(f"Unknown contrastive loss function: {contrastive_loss_fn}")

        if logsumexp_penalty > 0:
            logsumexp = jax.nn.logsumexp(logits, axis=1)
            loss += logsumexp_penalty * jnp.mean(logsumexp**2)

        I = jnp.eye(logits.shape[0])
        correct = jnp.argmax(logits, axis=1) == jnp.argmax(I, axis=1)
        logits_pos = jnp.sum(logits * I) / jnp.sum(I)
        logits_neg = jnp.sum(logits * (1 - I)) / jnp.sum(1 - I)
        if len(logits.shape) == 3:
            logsumexp = jax.nn.logsumexp(logits[:, :, 0], axis=1) ** 2
        else:
            logsumexp = jax.nn.logsumexp(logits, axis=1) ** 2
        metrics = {
            "binary_accuracy": jnp.mean((logits > 0) == I),
            "categorical_accuracy": jnp.mean(correct),
            "logits_pos": logits_pos,
            "logits_neg": logits_neg,
            "logsumexp": logsumexp.mean(),
        }

        return loss, metrics

    def actor_loss(
        policy_params: Params,
        normalizer_params: Any,
        # q_params: Params,
        crl_critic_params: Params,
        alpha: jnp.ndarray,
        transitions: Transition,
        key: PRNGKey,
    ) -> jnp.ndarray:
        state, goal = transitions.observation[:, :obs_dim], transitions.observation[:, obs_dim:]
        observation = jnp.concatenate((state, goal), axis=1)

        dist_params = policy_network.apply(normalizer_params, policy_params, observation)
        action = parametric_action_distribution.sample_no_postprocessing(dist_params, key)
        log_prob = parametric_action_distribution.log_prob(dist_params, action)
        action = parametric_action_distribution.postprocess(action)

        sa_encoder_params, g_encoder_params = (
            crl_critic_params["sa_encoder"],
            crl_critic_params["g_encoder"],
        )
        sa_repr = sa_encoder.apply(
            normalizer_params,
            sa_encoder_params,
            jnp.concatenate([state, action], axis=-1),
        )
        g_repr = g_encoder.apply(normalizer_params, g_encoder_params, goal)
        min_q = jnp.einsum("ik,ik->i", sa_repr, g_repr)

        actor_loss = alpha * log_prob - min_q
        return jnp.mean(actor_loss)

    return alpha_loss, actor_loss, crl_critic_loss