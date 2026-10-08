"""Offline suite: never select a paid model from a developer's .env file."""
import os

os.environ['LLM_PROVIDER'] = 'mock'
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
