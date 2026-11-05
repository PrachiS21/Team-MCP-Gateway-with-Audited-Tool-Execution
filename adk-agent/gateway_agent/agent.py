"""What `adk web` and `adk run` load: root_agent, built from the environment (see factory.py and README.md)."""

from gateway_agent.factory import agent_from_env

root_agent = agent_from_env()
