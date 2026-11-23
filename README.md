# Rasputin

**A local AI assistant platform that runs on your own hardware.**

Rasputin is an LLM harness with extra features built into it: voice and text chat, smart-light control, a Discord bot, an AI facial-recognition security system, image generation, and games you can play against the AI, including chess and Minecraft. Everything runs on a single consumer GPU with 8GB of VRAM at roughly **50–80 tokens per second**.

![License](https://img.shields.io/badge/license-MIT-blue)

---

## Table of Contents

- [Features](#features)
- [AI Models](#ai-models)
- [Tech Stack](#tech-stack)
- [Requirements](#requirements)
- [How It Works](#how-it-works)
- [Remote Demos with Port Tunneling](#remote-demos-with-port-tunneling)
- [Background](#background)
- [Third-Party Software](#third-party-software)
- [License](#license)

---

## Features

### Chat (text and voice)
- Talk to the assistant by typing or by speaking.
- Speech-to-text turns your voice into messages, and text-to-speech reads replies back.
- Voice conversion lets the assistant respond in a custom voice.

### Home tab (smart lights)
- Turn lights on and off.
- Change light colors.
- Works through voice commands or by asking the AI in the text chat.

### Security tab
- AI facial-recognition security system.
- Carried over from my first project, **GuardAI**, and rebuilt as a tab inside Rasputin.
- Uses face tracking to detect and follow faces.

### Image generation
- Generate images from text prompts, running locally on the same GPU.

### Games
- **Chess:** play against the AI, and it talks to you as you play.
- **Minecraft:** play alongside the AI using the Mindcraft tool.

### Discord bot
- Chat with the assistant from a Discord server.

### Port tunneling
- Optional port tunneling to share a running instance for demos.

---

## AI Models

Rasputin combines seven AI models, each handling one job.

| Capability | Model |
|---|---|
| Text and chat | Phi-4-mini-instruct |
| Image generation | DreamShaper 8 |
| Speech-to-text | Whisper |
| Text-to-speech | Piper |
| Voice conversion | RVC |
| Face tracking | MediaPipe Face Landmarker |
| Chess engine | Stockfish |

**Performance:** on a GPU with 8GB of VRAM, the chat model runs at about 50–80 tokens per second.

---

## Tech Stack

- **Python:** backend, model orchestration, and integrations
- **HTML, CSS, JavaScript:** web interface
- **SQL:** data storage

---

## Requirements

- A GPU with at least **8GB of VRAM** (the project was built and tested on an 8GB card)
- A microphone and speakers for the voice features
- A Discord bot token for the Discord bot
- Compatible smart lights for the Home tab

---

## How It Works

Rasputin runs a local language model (Phi-4-mini-instruct) as the "brain" and connects it to the other tools:

1. **You talk or type.** Speech goes through Whisper to become text, and typed messages go straight to the language model.
2. **The model decides what to do.** It can answer, change your lights, start a game, generate an image, or hand off to another feature.
3. **Replies come back as text or speech.** Piper reads responses aloud, and RVC can change the voice.
4. **Extra tabs add more.** The Home tab controls lights, the Security tab runs facial recognition with MediaPipe, and the games tab runs chess with Stockfish or Minecraft through Mindcraft.

The web interface is built with HTML, CSS, and JavaScript. The backend is written in Python and uses SQL for storage.

---

## Remote Demos with Port Tunneling

Rasputin can use **port tunneling** to make a locally running instance reachable from the internet. I mainly use it to show the project off without deploying it to a server.

> **Security note:** only expose Rasputin while you are actively demoing it, and don't leave the Home or Security tabs open to the public.

---

## Background

Rasputin grew out of my first project, **GuardAI**, an AI facial-recognition security system. I rebuilt that system as the **Security** tab and then expanded the project into a full local assistant with chat, voice, smart-home control, games, and a Discord bot.

---

## Third-Party Software

Rasputin builds on a lot of open-source work. Each tool below keeps its own license, so please check them before reusing or distributing anything.

- **Phi-4-mini-instruct** (Microsoft)
- **DreamShaper 8**
- **Whisper** (OpenAI)
- **Piper** (text-to-speech)
- **RVC** (Retrieval-based Voice Conversion)
- **MediaPipe Face Landmarker** (Google)
- **Stockfish** (chess engine)
- **Mindcraft** (Minecraft AI agent tool)

Model weights and binaries are not distributed with this repository.

---

## License

This project's code is released under the **MIT License**. Third-party models and tools are covered by their own licenses.
