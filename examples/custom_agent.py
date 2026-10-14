"""A custom agent loop with hypnos, using any OpenAI-compatible API (Ollama by default).

    ollama pull llama3.2
    uv run --with-editable ../hypnos python custom_agent.py
"""
import json
import os
import urllib.request

from hypnos.integrations.agents import AgentMemory

API = os.environ.get('OPENAI_BASE_URL', 'http://localhost:11434/v1') + '/chat/completions'
MODEL = os.environ.get('MODEL', 'llama3.2')
mem = AgentMemory('custom-agent', session='cli')


def chat(system, user):
    body = json.dumps({'model': MODEL, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}).encode()
    req = urllib.request.Request(API, data=body, headers={'Content-Type': 'application/json',
                                                          'Authorization': 'Bearer ' + os.environ.get('OPENAI_API_KEY', 'x')})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())['choices'][0]['message']['content']


print('Type a message (empty line to quit). Memory persists between runs.')
while True:
    user = input('you> ').strip()
    if not user:
        break
    system = mem.system_prompt('You are a helpful assistant with long-term memory.', user)
    mem.user(user)
    reply = chat(system, user)
    mem.assistant(reply)
    print('bot>', reply)
mem.sleep()
