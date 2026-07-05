"""
Рівень сервісів - реалізація STT, LLM та TTS.

Кожен сервіс є ізольованим та async-first для максимальної продуктивності.
Сервіси не знають про FastAPI або WebSockets - це чиста бізнес-логіка.
"""

import asyncio
import base64
import io
import logging
from typing import AsyncGenerator, List, Dict, Optional
from groq import AsyncGroq
import edge_tts

from server.config import config

logger = logging.getLogger(__name__)


class GroqSTTService:
    """
    Перетворення мови в текст (STT) за допомогою API Groq Whisper.
    
    Чому Groq? Їх інфраструктура забезпечує в 10 разів швидший логічний вивід, 
    ніж стандартний Whisper, що критично для реального часу.
    """
    
    def __init__(self):
        """Ініціалізація клієнта Groq з API ключем з конфігурації."""
        self.client = AsyncGroq(api_key=config.GROQ_API_KEY)
        self.model = config.STT_MODEL
        logger.info(f"STT Service initialized with model: {self.model}")
    
    async def transcribe(self, audio_base64: str) -> str:
        """
        Транскрибування аудіо з закодованих у base64 даних WAV.
        
        Args:
            audio_base64: Аудіо дані, закодовані в Base64
            
        Returns:
            Транскрибований текст
            
        Чому async? Мережевий ввід/вивід до API Groq заблокував би цикл подій,
        якщо був би синхронним. Async дозволяє обробляти кілька запитів одночасно.
        """
        try:
            # Декодування base64 в сирі байти
            audio_bytes = base64.b64decode(audio_base64)
            
            # Створення файлоподібного об'єкта для API
            # Чому io.BytesIO? API Groq очікує файлоподібний об'єкт,
            # але у нас є байти в пам'яті. BytesIO обгортає байти як файл.
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "audio.wav"  # API вимагає ім'я файлу
            
            # Виконання асинхронного запиту до API
            # await тут передає керування під час очікування відповіді мережі
            transcription = await self.client.audio.transcriptions.create(
                file=audio_file,
                model=self.model,
                response_format="text",  # Звичайний текст, не JSON
                language="en"  # Оптимізація для англійської
            )
            
            result = transcription.strip()
            logger.info(f"Transcribed: {result[:50]}...")
            return result
            
        except Exception as e:
            logger.error(f"STT Error: {e}", exc_info=True)
            raise RuntimeError(f"Transcription failed: {str(e)}")


class GroqLLMService:
    """
    Сервіс великої мовної моделі (LLM), що використовує надшвидкий логічний вивід Groq.
    
    Ключова особливість: Потокові відповіді для найнижчої затримки.
    Ми можемо почати обробку першого речення, поки генерується решта.
    """
    
    def __init__(self):
        """Ініціалізація клієнта Groq та налаштувань розмови."""
        self.client = AsyncGroq(api_key=config.GROQ_API_KEY)
        self.model = config.LLM_MODEL
        self.system_prompt = config.SYSTEM_PROMPT
        logger.info(f"LLM Service initialized with model: {self.model}")
    
    async def generate_streaming(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        sentence_queue: asyncio.Queue
    ) -> str:
        """
        Генерація відповіді LLM з потоковою передачею.
        
        Args:
            user_message: Поточне повідомлення користувача
            conversation_history: Контекст попередньої розмови
            sentence_queue: Черга для відправки завершених речень
            
        Returns:
            Повний текст відповіді
            
        Чому потокова передача? Традиційний підхід чекає повної відповіді (2-3с),
        потім запускає TTS. Потокова передача дозволяє запустити TTS після першого речення (~300мс).
        Це зменшує сприйману затримку на 50%+.
        """
        try:
            # Створення масиву повідомлень з системним промптом та історією
            messages = [{"role": "system", "content": self.system_prompt}]
            messages.extend(conversation_history)
            messages.append({"role": "user", "content": user_message})
            
            logger.info(f"Generating response for: {user_message[:50]}...")
            
            # Виконання потокового запиту до API
            # await тут встановлює з'єднання
            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,  # Баланс творчості та зв'язності
                max_tokens=1024,  # Розумний ліміт для голосових відповідей
                stream=True  # Увімкнути потокову передачу токенів
            )
            
            # Накопичення відповіді та виявлення меж речень
            full_response = ""
            current_sentence = ""
            
            # Обробка потоку по частинах
            # Чому async for? Потік є асинхронним ітератором, що видає частини
            async for chunk in stream:
                if not chunk.choices:
                    continue
                
                delta = chunk.choices[0].delta
                if not delta.content:
                    continue
                
                content = delta.content
                full_response += content
                current_sentence += content
                
                # Виявлення меж речень
                # Надсилання завершених речень негайно до TTS
                # Чому? TTS може почати працювати, поки ми генеруємо наступне речення
                if any(punct in content for punct in ['.', '!', '?', '\n']):
                    sentence = current_sentence.strip()
                    if sentence:
                        logger.debug(f"Sentence complete: {sentence[:50]}...")
                        # Покласти речення в чергу для обробки TTS
                        # await, оскільки Queue.put() є асинхронним
                        await sentence_queue.put(sentence)
                        current_sentence = ""
            
            # Надсилання будь-якого залишкового тексту
            if current_sentence.strip():
                await sentence_queue.put(current_sentence.strip())
            
            # Сигнал кінця відповіді за допомогою вартового None
            # Сервіс TTS побачить це і перестане чекати
            await sentence_queue.put(None)
            
            # Оновлення історії розмови для контексту
            conversation_history.append({"role": "user", "content": user_message})
            conversation_history.append({"role": "assistant", "content": full_response})
            
            # Підтримка історії в керованих межах (останні 10 повідомлень = 5 обмінів)
            # Чому ліміт? Запобігає проблемам з лімітом токенів і зберігає контекст актуальним
            if len(conversation_history) > 10:
                conversation_history[:] = conversation_history[-10:]
            
            logger.info(f"LLM response complete ({len(full_response)} chars)")
            return full_response
            
        except Exception as e:
            logger.error(f"LLM Error: {e}", exc_info=True)
            # Сигнал про помилку до TTS закриттям черги
            await sentence_queue.put(None)
            raise RuntimeError(f"LLM generation failed: {str(e)}")


class EdgeTTSService:
    """
    Перетворення тексту в мову (TTS) за допомогою Microsoft Edge TTS.
    
    Чому Edge TTS?
    - Безкоштовно без необхідності ключа API
    - Високоякісні нейронні голоси
    - Низька затримка (~100мс на речення)
    - Підтримка потокової передачі
    """
    
    def __init__(self):
        """Ініціалізація TTS з налаштованим голосом."""
        self.voice = config.TTS_VOICE
        logger.info(f"TTS Service initialized with voice: {self.voice}")
    
    async def generate_speech(self, text: str) -> AsyncGenerator[str, None]:
        """
        Генерація аудіо мовлення з тексту, видача (yield) повного MP3 файлу.
        
        Args:
            text: Текст для синтезу
            
        Yields:
            Повний MP3 файл, закодований у Base64
            
        Чому буферизація? miniaudio.decode() вимагає повні MP3 файли з правильними
        заголовками та EOF маркерами. Потокові фрагменти викликають помилки декодування.
        Буферизація на рівні речення додає ~200-400мс затримки, але забезпечує
        100% надійність відтворення.
        """
        try:
            logger.debug(f"Synthesizing: {text[:50]}...")
            
            # Створення комунікатора Edge TTS
            communicate = edge_tts.Communicate(text, self.voice)
            
            # Буферизація всіх фрагментів аудіо для цього речення
            # Чому bytearray? Ефективне накопичення бінарних даних
            audio_buffer = bytearray()
            
            # Збір усіх фрагментів MP3
            # async for, оскільки communicate.stream() є асинхронним генератором
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    # Додавання фрагмента до буфера
                    audio_buffer.extend(chunk["data"])
            
            # Передача повного MP3 файлу як одного blob
            if audio_buffer:
                # Конвертація у bytes та кодування в base64
                complete_mp3 = bytes(audio_buffer)
                audio_base64 = base64.b64encode(complete_mp3).decode('utf-8')
                
                logger.debug(f"✓ Complete MP3: {len(complete_mp3)} bytes for text: {text[:30]}...")
                
                # Відправка повного файлу
                yield audio_base64
            else:
                logger.warning(f"No audio data generated for text: {text[:50]}")
                    
        except Exception as e:
            logger.error(f"TTS Error: {e}", exc_info=True)
            raise RuntimeError(f"TTS generation failed: {str(e)}")