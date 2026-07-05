"""
Клієнт голосового асистента

Обробляє:
1. Запис аудіо з виявленням голосової активності (VAD)
2. WebSocket комунікацію (повнодуплексну)
3. Відтворення аудіо

Повний дуплекс: Надсилання та отримання відбуваються одночасно.
Ви можете говорити, поки асистент відповідає.
"""

import asyncio
import base64
import time
import json
import logging
import wave
import io
from typing import Optional
from collections import deque

import queue
import threading
import pyaudio
import webrtcvad
import websockets
import miniaudio

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class VoiceAssistantClient:
    """
    Клієнт для спілкування з голосовим асистентом у реальному часі.
    
    Архітектура:
    - Запис відбувається в окремому потоці (блокуючий PyAudio)
    - WebSocket комунікація є асинхронною
    - Відтворення відбувається у виконавці (блокуючий PyAudio)
    - Усе координується циклом подій asyncio
    """
    
    def __init__(
        self,
        server_url: str = "ws://localhost:8000/ws",
        sample_rate: int = 16000,
        chunk_duration_ms: int = 30,
        vad_aggressiveness: int = 2
    ):
        """
        Ініціалізація клієнта.
        
        Args:
            server_url: URL WebSocket сервера
            sample_rate: Частота дискретизації аудіо (16кГц для Whisper)
            chunk_duration_ms: Розмір фрагмента VAD (10, 20 або 30 мс)
            vad_aggressiveness: Чутливість VAD (0-3, вище = агресивніше)
        """
        self.server_url = server_url
        self.sample_rate = sample_rate
        
        # Обчислення розміру фрагмента для VAD
        # Чому конкретні розміри? VAD працює лише з фрагментами 10, 20 або 30 мс
        self.chunk_duration_ms = chunk_duration_ms
        self.chunk_size = int(sample_rate * chunk_duration_ms / 1000)
        
        # Ініціалізація PyAudio для запису/відтворення
        self.audio = pyaudio.PyAudio()
        
        # Оптимізація відтворення: Постійний потік та черга
        self.playback_queue = queue.Queue()
        self.output_stream = None
        self.playback_thread = threading.Thread(target=self._playback_worker, daemon=True)
        self.playback_thread.start()
        
        # Ініціалізація виявлення голосової активності (VAD)
        # Чому VAD? Фільтрує тишу, зменшує передачу даних на 60%+
        self.vad = webrtcvad.Vad(vad_aggressiveness)
        
        # WebSocket з'єднання
        self.websocket: Optional[websockets.WebSocketClientProtocol] = None
        
        # Прапорці стану
        self.is_playing = False
        self.is_connected = False
        
    def _playback_worker(self):
        """
        Фоновий потік для безперервного відтворення аудіо.
        
        Чому це краще?
        1. Відкриває потік лише один раз (економить час).
        2. Усуває затримки між фрагментами.
        3. Розвантажує основний цикл подій.
        
        Покращення v2.0:
        - Валідація MP3 заголовків
        - Детальна діагностика помилок
        - Автоматична реконфігурація потоку при зміні формату
        """
        while True:
            # Отримання даних з черги
            audio_data = self.playback_queue.get()
            
            # None означає сигнал зупинки
            if audio_data is None:
                break
                
            try:
                # Діагностика: розмір та заголовок
                logger.debug(f"Decoding {len(audio_data)} bytes, header: {audio_data[:10].hex() if len(audio_data) >= 10 else 'N/A'}")
                
                # Валідація: занадто малий розмір
                if len(audio_data) < 10:
                    logger.warning(f"Audio chunk too small: {len(audio_data)} bytes, skipping")
                    continue
                
                # Валідація: MP3 має починатися з ID3 тега або sync word (0xFFxx)
                if not (audio_data.startswith(b'ID3') or audio_data[0] == 0xFF):
                    logger.error(f"Invalid MP3 header: {audio_data[:4].hex()}, expected ID3 or 0xFFxx")
                    continue
                
                # Декодування MP3 в PCM (miniaudio)
                decoded = miniaudio.decode(
                    audio_data,
                    output_format=miniaudio.SampleFormat.SIGNED16
                )
                
                logger.debug(f"Decoded: {decoded.nchannels}ch, {decoded.sample_rate}Hz, {len(decoded.samples)} samples")
                
                # Ініціалізація або реконфігурація потоку
                # Перевірка: чи змінився формат аудіо?
                stream_needs_reset = (
                    self.output_stream is None or
                    self.output_stream._rate != decoded.sample_rate or
                    self.output_stream._channels != decoded.nchannels
                )
                
                if stream_needs_reset:
                    # Закриття старого потоку
                    if self.output_stream:
                        try:
                            self.output_stream.stop_stream()
                            self.output_stream.close()
                        except Exception as e:
                            logger.warning(f"Error closing old stream: {e}")
                    
                    # Відкриття нового потоку з правильними параметрами
                    self.output_stream = self.audio.open(
                        format=pyaudio.paInt16,
                        channels=decoded.nchannels,
                        rate=decoded.sample_rate,
                        output=True,
                        frames_per_buffer=1024
                    )
                    logger.info(f"✓ Opened audio stream: {decoded.nchannels}ch @ {decoded.sample_rate}Hz")
                
                # Запис у потік (відтворення)
                self.output_stream.write(decoded.samples.tobytes())
                
            except miniaudio.DecodeError as e:
                logger.error(f"MP3 decode failed: {e}")
                logger.error(f"Data size: {len(audio_data)}, header: {audio_data[:20].hex() if len(audio_data) >= 20 else 'N/A'}")
                
            except Exception as e:
                logger.error(f"Playback error: {e}", exc_info=True)
                
            finally:
                self.playback_queue.task_done()
                
        # Закриття потоку при виході
        if self.output_stream:
            try:
                self.output_stream.stop_stream()
                self.output_stream.close()
                self.output_stream = None
                logger.debug("Playback stream closed")
            except Exception as e:
                logger.warning(f"Error closing playback stream: {e}")
    
    async def connect(self):
        """
        Встановлення WebSocket з'єднання з сервером.
        
        Чому async? Встановлення з'єднання включає мережевий ввід/вивід.
        """
        try:
            logger.info(f"Connecting to {self.server_url}...")
            
            # await передає керування під час з'єднання
            # open_timeout: збільшено до 30с для обробки повільного запуску сервера
            self.websocket = await websockets.connect(
                self.server_url,
                open_timeout=30,       # Дозволяє повільний запуск сервера/моделей
                ping_interval=20,      # Надсилати ping кожні 20с
                ping_timeout=10,       # Тайм-аут, якщо немає pong через 10с
                close_timeout=10
            )
            
            self.is_connected = True
            logger.info("✓ Connected to server")
            logger.debug(f"WebSocket local: {self.websocket.local_address}, remote: {self.websocket.remote_address}")
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            raise
    
    async def disconnect(self):
        """Закриття WebSocket з'єднання коректно."""
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            logger.info("Disconnected from server")
    
    def record_audio_with_vad(self, max_duration_seconds: int = 10) -> bytes:
        """
        Запис аудіо з виявленням голосової активності.
        
        VAD фільтрація:
        - Виявляє, коли користувач починає говорити
        - Включає деяку тишу перед мовленням (кільцевий буфер)
        - Виявляє, коли користувач припиняє говорити
        - Зупиняє запис автоматично
        
        Args:
            max_duration_seconds: Максимальний час запису
            
        Returns:
            Байти аудіо у форматі WAV
            
        Чому блокуючий? Запис PyAudio за своєю суттю є блокуючим.
        Ми запускаємо це у виконавці з async коду, щоб уникнути блокування циклу подій.
        """
        logger.info("Recording... (speak now)")
        
        # Відкриття аудіо потоку
        # Чому ці параметри? Відповідають очікуванням Whisper (16кГц, моно, 16-біт)
        stream = self.audio.open(
            format=pyaudio.paInt16,  # 16-bit PCM
            channels=1,               # Mono
            rate=self.sample_rate,
            input=True,
            frames_per_buffer=self.chunk_size
        )
        
        frames_with_speech = []
        num_chunks = int(self.sample_rate / self.chunk_size * max_duration_seconds)
        
        # Кільцевий буфер зберігає останні кадри
        # Чому? Включає контекст перед початком мовлення (запобігає обрізанню слів)
        ring_buffer = deque(maxlen=10)
        triggered = False  # Чи виявили ми вже мовлення?
        
        try:
            for i in range(num_chunks):
                # Читання фрагмента аудіо
                data = stream.read(self.chunk_size, exception_on_overflow=False)
                
                # Перевірка наявності мовлення в цьому фрагменті
                # VAD вимагає конкретних частот дискретизації та розмірів фрагментів
                is_speech = self.vad.is_speech(data, self.sample_rate)
                
                if not triggered:
                    # Все ще чекаємо початку мовлення
                    ring_buffer.append((data, is_speech))
                    num_voiced = len([f for f, speech in ring_buffer if speech])
                    
                    # Спрацьовує, коли >50% буфера містить мовлення
                    # Чому 50%? Баланс між хибними срацьовуваннями та швидкою реакцією затримки
                    if num_voiced > 0.5 * ring_buffer.maxlen:
                        triggered = True
                        logger.info("Speech detected!")
                        
                        # Додавання буферизованих кадрів (включає контекст перед мовленням)
                        for frame, _ in ring_buffer:
                            frames_with_speech.append(frame)
                        ring_buffer.clear()
                else:
                    # Запис мовлення
                    frames_with_speech.append(data)
                    ring_buffer.append((data, is_speech))
                    num_unvoiced = len([f for f, speech in ring_buffer if not speech])
                    
                    # Зупинити, якщо >90% недавнього буфера - тиша
                    # Чому 90%? Гарантує, що ми захопимо кінець речення
                    if num_unvoiced > 0.9 * ring_buffer.maxlen:
                        logger.info("Silence detected, stopping...")
                        break
        
        finally:
            stream.stop_stream()
            stream.close()
        
        if not frames_with_speech:
            logger.warning("No speech detected")
            return b""
        
        # Конвертація кадрів у формат WAV
        # Чому WAV? Нестиснений, широко підтримуваний, Whisper очікує його
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(self.audio.get_sample_size(pyaudio.paInt16))
            wf.setframerate(self.sample_rate)
            wf.writeframes(b''.join(frames_with_speech))
        
        wav_bytes = wav_buffer.getvalue()
        logger.info(f"✓ Recorded {len(wav_bytes)} bytes")
        return wav_bytes
    
    async def send_audio(self, audio_data: bytes):
        """
        Надсилання аудіо даних на сервер.
        
        Args:
            audio_data: Байти аудіо WAV
            
        Чому base64? WebSocket JSON не може обробляти сирі бінарні дані.
        """
        if not self.websocket or not self.is_connected:
            raise ConnectionError("Not connected to server")
        
        # Кодування в base64 для передачі JSON
        audio_base64 = base64.b64encode(audio_data).decode('utf-8')
        
        # Надсилання як JSON
        await self.websocket.send(json.dumps({
            "type": "audio",
            "data": audio_base64
        }))
        
        logger.info("Audio sent to server")
    
    def play_audio(self, audio_base64: str):
        """
        Додавання аудіо в чергу відтворення.
        
        Args:
            audio_base64: Аудіо, закодоване в Base64
            
        Чому це швидко? Просто декодує base64 і кладе в чергу.
        Ми НЕ чекаємо завершення відтворення тут.
        """
        try:
            # Декодування base64 (отримуємо MP3 байти)
            audio_data = base64.b64decode(audio_base64)
            
            # Додавання в чергу (потік відтворення забере це)
            self.playback_queue.put(audio_data)
            
        except Exception as e:
            logger.error(f"Queue error: {e}")
    
    async def listen_for_responses(self):
        """
        Прослуховування повідомлень сервера (запускається одночасно).
        
        Це частина "отримання" повнодуплексної комунікації.
        Вона працює безперервно у фоновому режимі.
        
        Чому async? Отримання WebSocket залежить від вводу/виводу.
        """
        if not self.websocket:
            raise ConnectionError("Not connected")
        
        try:
            # Безперервне отримання повідомлень
            # async for робить websocket асинхронним ітератором
            async for message in self.websocket:
                data = json.loads(message)
                msg_type = data.get("type")
                
                if msg_type == "transcription":
                    # Відображення того, що сказав користувач
                    logger.info(f"You said: {data['data']}")
                
                elif msg_type == "text":
                    # Відображення тексту відповіді асистента
                    logger.info(f"Assistant: {data['data']}")
                
                elif msg_type == "audio":
                    # Відтворення аудіо асистента
                    # Запуск у виконавці, щоб уникнути блокування циклу подій
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(
                        None,  # Використовувати стандартний виконавець
                        self.play_audio,
                        data['data']
                    )
                
                elif msg_type == "done":
                    logger.info("Response complete\n")
                    self.is_playing = False
                
                elif msg_type == "error":
                    logger.error(f"Error: {data.get('message')}\n")
                    self.is_playing = False
        
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Connection closed by server")
            self.is_connected = False
        
        except Exception as e:
            logger.error(f"Error receiving messages: {e}")
            self.is_connected = False
    
    async def run_conversation_loop(self):
        """
        Головний цикл розмови (повний дуплекс).
        
        Повний дуплекс означає:
        - Завдання прослуховування працює безперервно у фоновому режимі
        - Головний цикл чекає на ввід користувача та надсилає аудіо
        - Обидва відбуваються одночасно
        
        Це частина "надсилання" повнодуплексної комунікації.
        """
        logger.info("\n" + "="*60)
        logger.info("Voice Assistant Client")
        logger.info("="*60)
        logger.info("Instructions:")
        logger.info("1. Press Enter to start recording")
        logger.info("2. Speak clearly into your microphone")
        logger.info("3. Stop speaking (auto-detects silence)")
        logger.info("4. Wait for response")
        logger.info("5. Press Ctrl+C to exit")
        logger.info("="*60 + "\n")
        
        # Запуск фонового слухача
        # asyncio.create_task планує його виконання одночасно
        listener_task = asyncio.create_task(self.listen_for_responses())
        
        try:
            while self.is_connected:
                # Очікування натискання Enter користувачем
                # Чому to_thread? input() блокує, це заморозило б цикл подій
                await asyncio.to_thread(
                    input,
                    "Press Enter to speak (or Ctrl+C to quit)... "
                )
                
                if not self.is_connected:
                    break
                
                # Запис аудіо у виконавці (блокуюча операція)
                loop = asyncio.get_event_loop()
                audio_data = await loop.run_in_executor(
                    None,
                    self.record_audio_with_vad,
                    10  # максимум 10 секунд
                )
                
                if not audio_data:
                    logger.warning("No audio recorded, try again\n")
                    continue
                
                # Надсилання на сервер
                self.is_playing = True
                await self.send_audio(audio_data)
                
                # Очікування завершення відповіді
                # Чому цикл? Підтримувати головний потік живим, поки відтворюється відповідь
                while self.is_playing and self.is_connected:
                    await asyncio.sleep(0.1)
        
        except KeyboardInterrupt:
            logger.info("\n\n Shutting down...")
        
        finally:
            # Очищення завдання слухача
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass
    
    def cleanup(self):
        """
        Комплексна очистка всіх аудіо ресурсів та потоків.
        
        Покращення v2.0:
        - Індивідуальна обробка помилок для кожного ресурсу
        - Гарантована очистка навіть при винятках
        - Детальне логування процесу
        """
        logger.info("Starting cleanup...")
        
        # 1. Зупинка потоку відтворення
        try:
            self.playback_queue.put(None)
            if hasattr(self, 'playback_thread') and self.playback_thread.is_alive():
                self.playback_thread.join(timeout=2.0)
                if self.playback_thread.is_alive():
                    logger.warning("⚠ Playback thread did not terminate cleanly")
                else:
                    logger.debug("✓ Playback thread stopped")
        except Exception as e:
            logger.error(f"Error stopping playback thread: {e}")
        
        # 2. Закриття output stream
        try:
            if self.output_stream:
                self.output_stream.stop_stream()
                self.output_stream.close()
                self.output_stream = None
                logger.debug("✓ Output stream closed")
        except Exception as e:
            logger.error(f"Error closing output stream: {e}")
        
        # 3. Завершення PyAudio
        try:
            self.audio.terminate()
            logger.debug("✓ PyAudio terminated")
        except Exception as e:
            logger.error(f"Error terminating PyAudio: {e}")
        
        logger.info("✓ Cleanup complete")


async def main():
    """
    Головна точка входу.
    
    Чому async? Весь клієнт асинхронний для неблокуючого вводу/виводу.
    """
    # Ініціалізація клієнта
    client = VoiceAssistantClient(
        server_url="ws://localhost:8000/ws",
        sample_rate=16000,
        vad_aggressiveness=2
    )
    
    try:
        # Підключення до сервера
        await client.connect()
        
        # Запуск циклу розмови
        await client.run_conversation_loop()
    
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
    
    finally:
        # Очищення
        await client.disconnect()
        client.cleanup()


if __name__ == "__main__":
    while True:
        try:
            print("\nConnecting to server...")
            asyncio.run(main())
        except KeyboardInterrupt:
            print("\nExiting...")
            break
        except Exception as e:
            print(f"\nConnection lost: {e}")
            print("Reconnecting in 3 seconds...")
            time.sleep(3)