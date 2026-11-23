# Rasputin

**A local AI assistant platform that runs on your own hardware.**

Rasputin is an LLM harness with extra features built into it: voice and text chat, smart-light control, a Discord bot, an AI facial-recognition security system, image generation, and games you can play against the AI, including chess and Minecraft. Everything runs on a single consumer GPU (8GB of VRAM) at roughly **50–80 tokens per second**.

![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.x-blue)
![Status](https://img.shields.io/badge/status-in%20development-orange)

<!-- Add a screenshot or GIF here, for example: ![Rasputin demo](docs/demo.gif) -->

---

## Table of Contents

- [Features](#features)
- [AI Models](#ai-models)
- [Tech Stack](#tech-stack)
- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Project Structure](#project-structure)
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
- Python 3.x `[add exact version]`
- `[Operating system you tested on]`
- A Discord bot token, if you want the Discord bot `[optional]`
- Smart lights compatible with `[add your light brand or protocol]`, if you want the Home tab
- Microphone and speakers, if you want voice features

---

## Installation

> Fill in these steps with the exact commands you use.

1. **Clone the repository**
   ```bash
   git clone https://github.com/[your-username]/[repo-name].git
   cd [repo-name]
   ```

2. **Create a virtual environment and install dependencies**
   ```bash
   [your commands here]
   ```

3. **Download the models**

   Model files are not included in this repository. Download each one from its official source and place it in the folder noted below.

   | Model | Where to get it | Put it in |
   |---|---|---|
   | Phi-4-mini-instruct | `[link]` | `[folder]` |
   | DreamShaper 8 | `[link]` | `[folder]` |
   | Whisper | `[link]` | `[folder]` |
   | Piper voice | `[link]` | `[folder]` |
   | RVC voice model | `[link]` | `[folder]` |
   | Stockfish | `[link]` | `[folder]` |

4. **Configure settings**
   ```bash
   [copy the example config, add your Discord token, light settings, etc.]
   ```

---

## Usage

1. **Start the app**
   ```bash
   [your start command]
   ```
2. Open the web interface at `[address, for example http://localhost:5000]`.
3. Use the tabs:
   - **Chat:** type or speak to the assistant.
   - **Home:** control your lights by voice or text.
   - **Security:** start the facial-recognition system.
   - **Games:** start a chess game, or launch Minecraft with Mindcraft.
4. To use the Discord bot, `[add the command to start it]` and invite the bot to your server.

### Example commands
- "Turn on the lights."
- "Make the lights blue."
- "Let's play chess."

---

## Project Structure

> Replace with your real folder layout.

```
[repo-name]/
├── [backend folder]/      # Python backend and model code
├── [web folder]/          # HTML, CSS, and JavaScript interface
├── [security folder]/     # Facial-recognition system (from GuardAI)
├── [config file]
└── README.md
```

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

This project's code is released under the **MIT License**. See the [LICENSE](LICENSE) file for details. Third-party models and tools are covered by their own licenses.

---

## Author

**Haden Hunt**
[LinkedIn](https://www.linkedin.com/in/haden-hunt-3000ba357)
