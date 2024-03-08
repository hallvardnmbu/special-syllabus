"""
Orion HPC training script.

Value-based vision agent in the tetris environment using PyTorch
"""

import re
import csv
import glob
import copy
import time
import random
import logging

import torch
import gymnasium as gym

from agent import VisionDeepQ

# Logging
# --------------------------------------------------------------------------------------------------

handler = logging.FileHandler('./output/info.txt')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(handler)

# Environment
# --------------------------------------------------------------------------------------------------

environment = gym.make('ALE/Tetris-v5', render_mode="rgb_array",
                       obs_type="grayscale", frameskip=1, repeat_action_probability=0.0)
environment.metadata["render_fps"] = 30

# Parameters
# --------------------------------------------------------------------------------------------------
# Description of the parameters:
#   SKIP : number of frames to skip between each saved frame
#   SHAPE : how to reshape the `original` image
#   DISCOUNT : discount rate for rewards
#   GAMMA : discount rate for Q-learning
#   GRADIENTS : clamp the gradients between these values (or None for no clamping)
#   PUNISHMENT : punishment for losing
#   INCENTIVE : incentive for rewards
#   EXPLORATION_RATE : initial exploration rate
#   EXPLORATION_MIN : minimum exploration rate
#   EXPLORATION_STEPS : number of games to decay exploration rate from `RATE` to `MIN`
#   MINIBATCH : size of the minibatch
#   TRAIN_EVERY : train the network every n games
#   START_TRAINING_AT : start training after n games
#   REMEMBER : only remember games with rewards, and this fraction of the games without
#   MEMORY : size of the agents internal memory
#   RESET_Q_EVERY : update target-network every n games

GAMES = 50000
SKIP = 4
CHECKPOINT = 5000

SHAPE = {
    "original": (1, 1, 210, 160),

    "height": slice(27, 203),
    "width": slice(22, 64),
    "max_pooling": 2,
}

DISCOUNT = 0.99
GAMMA = 0.99
GRADIENTS = (-1, 1)

PUNISHMENT = -10
INCENTIVE = 10

MINIBATCH = 32
TRAIN_EVERY = 5
START_TRAINING_AT = 1000

EXPLORATION_RATE = 1.0
EXPLORATION_MIN = 0.001
EXPLORATION_STEPS = 20000 // TRAIN_EVERY

REMEMBER = 0.0025
MEMORY = 500
RESET_Q_EVERY = TRAIN_EVERY * 250

# These network- and optimizer-parameters were based on the suggestion by Claude.
# (Chat: https://claude.ai/chat/024bd66a-448c-4430-acb6-b3444d8170bf)

NETWORK = {
    "input_channels": 4, "outputs": 5,
    "channels": [64, 64, 32],
    "kernels": [5, 3, 3],
    "padding": ["same", "same", "same"],
    "strides": [],
    "nodes": [64],
}
OPTIMIZER = {
    "optimizer": torch.optim.RMSprop,
    "lr": 0.00025,
    "hyperparameters": {"alpha": 0.99, "eps": 1e-8}
}

METRICS = "./output/metrics.csv"

# Initialisation
# --------------------------------------------------------------------------------------------------
# Searches for the pattern "weights-{CHECKPOINT}.pth" in the current directory and
# subdirectories, and loads the weights from the file with the highest checkpoint.

logger.info("Initialising agent")
value_agent = VisionDeepQ(
    network=NETWORK, optimizer=OPTIMIZER, shape=SHAPE,

    batch_size=MINIBATCH, memory=MEMORY,

    discount=DISCOUNT, gamma=GAMMA,
    punishment=PUNISHMENT, incentive=INCENTIVE,

    exploration_rate=EXPLORATION_RATE,
    exploration_steps=EXPLORATION_STEPS,
    exploration_min=EXPLORATION_MIN,
)
logger.info(value_agent.eval())

files = glob.glob("**/*.pth", recursive=True)
if files:
    for file in sorted(files, key=lambda x: int(re.search(r'/weights-(\d+).pth', x).group(1))
                       if re.search(r'/weights-(\d+).pth', x)
                       else 0, reverse=True):
        try:
            weights = torch.load(file, map_location=value_agent.device)
            value_agent.load_state_dict(weights)
            logger.info("Weights loaded from %s", file)
            break
        except RuntimeError as e:
            logger.error("Failed to load weights from %s due to error: %s", file, str(e))

_value_agent = copy.deepcopy(value_agent)

with open(METRICS, "w", newline="", encoding="UTF-8") as file:
    metric = csv.writer(file)
    metric.writerow(["game", "steps", "loss", "exploration", "reward"])

# Training
# --------------------------------------------------------------------------------------------------

logger.info("Starting playing")
start = time.time()

TRAINING = False
_STEPS = _LOSS = _REWARD = 0
for game in range(1, GAMES + 1):

    initial = value_agent.preprocess(environment.reset()[0])
    states = torch.cat([initial] * value_agent.shape["reshape"][1], dim=1)

    DONE = False
    STEPS = REWARDS = 0
    TRAINING = True if (not TRAINING and game >= START_TRAINING_AT) else TRAINING
    while not DONE:
        action, new_states, rewards, DONE = value_agent.observe(environment, states, skip=SKIP)
        value_agent.remember(states, action, rewards)

        states = new_states
        REWARDS += rewards.item()
        STEPS += 1

    if random.random() < REMEMBER or REWARDS > 0:
        value_agent.memorize(states, STEPS)
        logger.info("  %s > Rewards: %s Steps: %s Memory: %s %%",
                    game, int(REWARDS), STEPS,
                    int(len(value_agent.memory["memory"]) * 100 / MEMORY))
    value_agent.memory["game"].clear()

    LOSS = None
    if game % TRAIN_EVERY == 0 and len(value_agent.memory["memory"]) > 0 and TRAINING:
        LOSS = value_agent.learn(network=_value_agent, clamp=GRADIENTS)
        EXPLORATION_RATE = value_agent.parameter["rate"]
        _LOSS += LOSS
    _REWARD += REWARDS
    _STEPS += STEPS

    if game % RESET_Q_EVERY == 0 and TRAINING:
        logger.info(" Resetting target-network")
        _value_agent.load_state_dict(value_agent.state_dict())

    # METRICS
    # ----------------------------------------------------------------------------------------------
    # Saves the metrics to a CSV file. Logs the progress of the training and saves the current
    # weights every `CHECKPOINT` games.

    with open(METRICS, "a", newline="", encoding="UTF-8") as file:
        metric = csv.writer(file)
        metric.writerow([game, STEPS, LOSS, EXPLORATION_RATE, REWARDS])

    if game % (CHECKPOINT // 2) == 0 or game == GAMES:
        logger.info("Game %s (progress %s %%, random %s %%)",
                    game, int(game * 100 / GAMES), round(EXPLORATION_RATE * 100, 2))
        logger.info(" > Average steps: %s", int(_STEPS / (CHECKPOINT // 2)))
        logger.info(" > Average loss:  %s", _LOSS / ((CHECKPOINT // 2) / TRAIN_EVERY))
        logger.info(" > Rewards:       %s", _REWARD)
        _STEPS = _LOSS = _REWARD = 0

    if TRAINING and game % CHECKPOINT == 0:
        logger.info("Saving weights")
        torch.save(value_agent.state_dict(), f"./output/weights-{game}.pth")

logger.info("Total training time: %s seconds", round(time.time() - start, 2))
logger.info("Metrics saved to %s", METRICS)