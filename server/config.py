"""
Управління конфігурацією
Завантажує та перевіряє всі змінні середовища в одному місці.
Це централізує конфігурацію та полегшує додавання нових налаштувань.
"""

import os
from dotenv import load_dotenv
from typing import Optional

# Завантаження змінних середовища з файлу .env
# Це потрібно викликати перед доступом до будь-яких змінних середовища
load_dotenv()


class Config:
    """
    Централізований клас конфігурації.
    
    Чому клас? Він забезпечує:
    - Підказки типів для підтримки IDE
    - Валідацію в одному місці
    - Легке тестування (мокування класу)
    - Чітку документацію всіх налаштувань
    """
    
    # Конфігурація API
    GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
    
    # Конфігурація сервера
    SERVER_HOST: str = os.getenv("SERVER_HOST", "0.0.0.0")
    SERVER_PORT: int = int(os.getenv("SERVER_PORT", "8000"))
    
    # Конфігурація моделі
    STT_MODEL: str = os.getenv("STT_MODEL", "whisper-large-v3")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
    TTS_VOICE: str = os.getenv("TTS_VOICE", "uk-UA-PolinaNeural")
    
    # Конфігурація аудіо
    SAMPLE_RATE: int = int(os.getenv("SAMPLE_RATE", "16000"))
    
    # WebSocket конфігурація
    WEBSOCKET_CONNECT_TIMEOUT: int = int(os.getenv("WEBSOCKET_CONNECT_TIMEOUT", "30"))
    WEBSOCKET_PING_INTERVAL: int = int(os.getenv("WEBSOCKET_PING_INTERVAL", "20"))
    WEBSOCKET_PING_TIMEOUT: int = int(os.getenv("WEBSOCKET_PING_TIMEOUT", "10"))
    
    # Конфігурація відтворення
    AUDIO_CHUNK_SIZE: int = int(os.getenv("AUDIO_CHUNK_SIZE", "1024"))
    PLAYBACK_BUFFER_SIZE: int = int(os.getenv("PLAYBACK_BUFFER_SIZE", "10"))
    
    # Системний промпт LLM
    SYSTEM_PROMPT: str = """You are a helpful, friendly voice assistant. You communicate exclusively in Ukrainian.
Keep your responses concise and conversational (2-3 sentences max unless asked for more detail).
Speak naturally as if in a real conversation."""
    
    @classmethod
    def validate(cls) -> None:
        """
        Перевірка необхідної конфігурації.
        Викликайте це при запуску, щоб швидко виявити помилку, якщо конфігурація відсутня.
        """
        import logging
        logger = logging.getLogger(__name__)
        
        if not cls.GROQ_API_KEY:
            raise ValueError(
                "GROQ_API_KEY is required. "
                "Please set it in your .env file. "
                "Get your key from: https://console.groq.com/keys"
            )
        
        if cls.SERVER_PORT < 1 or cls.SERVER_PORT > 65535:
            raise ValueError(f"Invalid SERVER_PORT: {cls.SERVER_PORT}")
        
        logger.info("Configuration validated successfully")


# Створення екземпляра-одинака (singleton)
config = Config()