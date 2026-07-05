"""
FastAPI сервер - Головна точка входу

Цей файл обробляє:
1. WebSocket з'єднання
2. Оркестрацію конвеєра (pipeline)
3. Обробку помилок та логування

Бізнес-логіка знаходиться в services.py - цей файл лише з'єднує все разом.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from server.config import config
from server.services import GroqSTTService, GroqLLMService, EdgeTTSService

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Ініціалізація сервісів глобально
# Це одинаки (singletons), спільні для всіх з'єднань
stt_service = GroqSTTService()
llm_service = GroqLLMService()
tts_service = EdgeTTSService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Сучасний менеджер життєвого циклу застосунку.
    
    Чому lifespan? Замінює deprecated @app.on_event("startup"/"shutdown").
    Забезпечує правильне управління ресурсами та коректне завершення роботи.
    """
    # Startup
    logger.info("=" * 60)
    logger.info("Voice Assistant Server Starting...")
    logger.info("=" * 60)
    
    # Валідація конфігурації
    config.validate()
    
    logger.info(f"STT Model: {config.STT_MODEL}")
    logger.info(f"LLM Model: {config.LLM_MODEL}")
    logger.info(f"TTS Voice: {config.TTS_VOICE}")
    logger.info(f"Server: http://{config.SERVER_HOST}:{config.SERVER_PORT}")
    logger.info("=" * 60)
    logger.info("✓ Server ready to accept connections")
    
    yield
    
    # Shutdown
    logger.info("=" * 60)
    logger.info("Voice Assistant Server Shutting Down...")
    logger.info("=" * 60)


# Ініціалізація FastAPI додатку з lifespan
app = FastAPI(
    title="Real-Time Voice Assistant",
    version="2.0.0",
    description="Production-grade voice assistant with streaming pipeline",
    lifespan=lifespan
)

# Додавання CORS middleware
# Чому? Дозволяє веб-клієнтам з різних джерел підключатися
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def health_check():
    """
    Ендпоінт перевірки здоров'я для моніторингу.
    Повертає статус сервера та конфігурацію.
    """
    return {
        "status": "healthy",
        "service": "Voice Assistant",
        "models": {
            "stt": config.STT_MODEL,
            "llm": config.LLM_MODEL,
            "tts": config.TTS_VOICE
        }
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Головний WebSocket ендпоінт для голосового асистента.
    
    Протокол:
    Клієнт → Сервер:
        {"type": "audio", "data": "<base64>"}
        {"type": "ping"}
    
    Сервер → Клієнт:
        {"type": "transcription", "data": "<текст>"}
        {"type": "text", "data": "<речення_відповіді>"}
        {"type": "audio", "data": "<base64>"}
        {"type": "done"}
        {"type": "error", "message": "<помилка>"}
    
    Чому WebSocket? Постійне двонаправлене з'єднання з низькими накладними витратами.
    Ідеально підходить для потокової передачі даних у реальному часі.
    """
    # Прийом WebSocket з'єднання
    # await, оскільки це включає мережевий ввід/вивід
    await websocket.accept()
    
    client_id = id(websocket)  # Простий ID для логування
    client_info = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "unknown"
    logger.info(f"✓ Client {client_id} connected from {client_info}")
    logger.debug(f"WebSocket state: {websocket.client_state}")
    
    # Історія розмови (унікальна для кожного з'єднання)
    # Чому для кожного з'єднання? Кожен користувач має свій контекст розмови
    conversation_history = []
    
    try:
        # Головний цикл повідомлень
        while True:
            # Отримання повідомлення від клієнта
            # await передає керування під час очікування даних
            message = await websocket.receive_json()
            message_type = message.get("type")
            
            if message_type == "audio":
                # Обробка голосового вводу
                audio_data = message.get("data")
                
                if not audio_data:
                    logger.warning(f"Client {client_id} sent empty audio")
                    await websocket.send_json({
                        "type": "error",
                        "message": "Empty audio data"
                    })
                    continue
                
                # Запуск повного конвеєра: STT → LLM → TTS
                await process_voice_pipeline(
                    websocket=websocket,
                    audio_data=audio_data,
                    conversation_history=conversation_history
                )
            
            elif message_type == "ping":
                # Відповідь на ping для підтримки з'єднання
                await websocket.send_json({"type": "pong"})
            
            else:
                logger.warning(f"Unknown message type: {message_type}")
    
    except WebSocketDisconnect:
        logger.info(f"✗ Client {client_id} disconnected gracefully")
    
    except Exception as e:
        logger.error(f"Error with client {client_id}: {e}", exc_info=True)
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"Server error: {str(e)}"
            })
        except:
            pass  # Клієнт міг відключитися
    
    finally:
        # Гарантована очистка з'єднання
        try:
            await websocket.close()
            logger.debug(f"WebSocket closed for client {client_id}")
        except:
            pass


async def process_voice_pipeline(
    websocket: WebSocket,
    audio_data: str,
    conversation_history: list
):
    """
    Оркестрація повного конвеєра голосового асистента.
    
    Етапи конвеєра:
    1. STT: Аудіо → Текст
    2. LLM: Текст → Відповідь (потокова)
    3. TTS: Відповідь → Аудіо (одночасно з LLM)
    
    Ключова оптимізація: LLM та TTS працюють одночасно.
    Як тільки LLM генерує речення, TTS починає його обробку.
    Це перекриття зменшує загальну затримку на 30-40%.
    """
    try:
        # ===== ЕТАП 1: Перетворення мови в текст (STT) =====
        logger.info("🎤 Stage 1: Transcribing audio...")
        
        # await, оскільки STT є асинхронним (мережевий ввід/вивід)
        transcription = await stt_service.transcribe(audio_data)
        
        if not transcription or not transcription.strip():
            logger.warning("Empty transcription received")
            await websocket.send_json({
                "type": "error",
                "message": "Could not understand audio. Please try again."
            })
            return
        
        logger.info(f"Transcription: {transcription}")
        
        # Надсилання транскрипції клієнту
        await websocket.send_json({
            "type": "transcription",
            "data": transcription
        })
        
        # ===== ЕТАП 2 та 3: LLM + TTS (Одночасно) =====
        logger.info("Stage 2: Generating response...")
        logger.info("Stage 3: Synthesizing speech...")
        
        # Створення черги для передачі речень
        # Чому Queue? Це безпечний для потоків спосіб передачі речень від LLM до TTS
        # LLM кладе речення → TTS отримує речення → обидва працюють одночасно
        sentence_queue = asyncio.Queue()
        
        # Створення одночасних завдань
        # asyncio.create_task планує виконання корутин "у фоновому режимі"
        # не чекаючи їх завершення
        llm_task = asyncio.create_task(
            llm_service.generate_streaming(
                user_message=transcription,
                conversation_history=conversation_history,
                sentence_queue=sentence_queue
            )
        )
        
        tts_task = asyncio.create_task(
            stream_tts_responses(websocket, sentence_queue)
        )
        
        # Очікування завершення обох завдань
        # asyncio.gather запускає їх одночасно і чекає завершення всіх
        await asyncio.gather(llm_task, tts_task, return_exceptions=True)
        
        # Сигнал завершення
        await websocket.send_json({"type": "done"})
        logger.info("Pipeline complete")
        
    except Exception as e:
        logger.error(f"Pipeline error: {e}", exc_info=True)
        await websocket.send_json({
            "type": "error",
            "message": f"Processing error: {str(e)}"
        })


async def stream_tts_responses(websocket: WebSocket, sentence_queue: asyncio.Queue):
    """
    Споживання речень з черги та генерація аудіо.
    
    Це працює одночасно з генерацією LLM.
    Як тільки LLM створює речення, це запускає TTS.
    
    Чому окрема функція? Чисте розділення обов'язків.
    LLM створює, TTS споживає - класичний шаблон виробник-споживач.
    """
    try:
        while True:
            # Очікування наступного речення від LLM
            # await, оскільки Queue.get() є асинхронним
            sentence = await sentence_queue.get()
            
            # None - це наше значення-вартовий, що означає "більше немає речень"
            if sentence is None:
                logger.debug("TTS stream complete")
                break
            
            logger.info(f"🔊 Synthesizing: {sentence[:50]}...")
            
            # Надсилання тексту клієнту для відображення
            await websocket.send_json({
                "type": "text",
                "data": sentence
            })
            
            # Генерація та потокова передача фрагментів аудіо
            # async for, оскільки generate_speech є асинхронним генератором
            async for audio_chunk in tts_service.generate_speech(sentence):
                await websocket.send_json({
                    "type": "audio",
                    "data": audio_chunk
                })
            
    except Exception as e:
        logger.error(f"TTS streaming error: {e}", exc_info=True)
        raise


# Точка входу
if __name__ == "__main__":
    import uvicorn
    
    # Запуск сервера
    # Чому uvicorn? Це швидкий ASGI сервер, оптимізований для асинхронного Python
    uvicorn.run(
        app,
        host=config.SERVER_HOST,
        port=config.SERVER_PORT,
        log_level="info",
        access_log=True
    )