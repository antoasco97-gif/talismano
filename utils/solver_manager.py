import logging
import asyncio
import json
import os
import aiohttp
from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, FLARESOLVERR_WARM_SESSIONS, WARP_PROXY_URL

logger = logging.getLogger(__name__)

class SolverSessionManager:
    """
    Gestore intelligente delle sessioni FlareSolverr.
    Supporta sessioni persistenti (Warm Mode) per risparmiare RAM o sessioni temporanee per risparmiare RAM.
    """
    _instance = None
    _persistent_sessions = {} # {key: session_id}
    _sessions_file = "persistent_sessions.json"
    _lock = asyncio.Lock()
    _initialized = False

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SolverSessionManager, cls).__new__(cls)
        return cls._instance

    async def _init_if_needed(self):
        if self._initialized: return
        async with self._lock:
            if self._initialized: return
            if os.path.exists(self._sessions_file):
                try:
                    with open(self._sessions_file, 'r') as f:
                        self._persistent_sessions = json.load(f)
                    logger.info(f"FlareSolverr: Caricate {len(self._persistent_sessions)} sessioni persistenti dal file.")
                except Exception as e:
                    logger.warning(f"FlareSolverr: Errore caricamento sessioni: {e}")
            self._initialized = True

    def _save_sessions(self):
        try:
            with open(self._sessions_file, 'w') as f:
                json.dump(self._persistent_sessions, f)
        except Exception as e:
            logger.warning(f"FlareSolverr: Errore salvataggio sessioni: {e}")

    def _warm_session_key(self, proxy: str = None) -> str:
        if not proxy:
            return "universal"
        return f"universal:{proxy}"

    def _normalize_proxy(self, proxy: str | None) -> str | None:
        if not proxy:
            return None
        return proxy.replace("socks5h://", "socks5://", 1)

    def _is_warm_proxy_eligible(self, proxy: str = None) -> bool:
        if proxy is None:
            return True
        return self._normalize_proxy(proxy) == self._normalize_proxy(WARP_PROXY_URL)

    async def get_session(self, proxy: str = None) -> tuple[str, bool]:
        """
        Ottiene una sessione FlareSolverr.
        Ritorna una tupla (session_id, is_persistent).
        """
        await self._init_if_needed()
        if not FLARESOLVERR_URL:
            return None, False

        # Se non c'è proxy e la modalità Warm è attiva, usiamo la sessione persistente
        if FLARESOLVERR_WARM_SESSIONS and self._is_warm_proxy_eligible(proxy):
            session_id = await self.get_persistent_session(self._warm_session_key(proxy), proxy)
            if session_id:
                return session_id, True
        
        # Altrimenti creiamo una sessione temporanea (sarà distrutta dopo l'uso)
        session_id = await self._create_session(proxy)
        return session_id, False

    async def get_persistent_session(self, key: str, proxy: str = None) -> str:
        """Ottiene o crea una sessione persistente identificata da una chiave (es. dominio)."""
        await self._init_if_needed()
        if not FLARESOLVERR_URL:
            return None
            
        async with self._lock:
            if key in self._persistent_sessions:
                sid = self._persistent_sessions[key]
                # Verifichiamo se la sessione esiste ancora su FlareSolverr
                if await self._session_exists(sid):
                    return sid
                logger.info(f"FlareSolverr: Sessione {sid} per {key} non più valida o scaduta.")
            
            logger.info(f"FlareSolverr: Creazione nuova sessione persistente per chiave: {key}")
            session_id = await self._create_session(proxy)
            if session_id:
                self._persistent_sessions[key] = session_id
                self._save_sessions()
            return session_id

    async def _session_exists(self, session_id: str) -> bool:
        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {"cmd": "sessions.list"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(endpoint, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return session_id in data.get("sessions", [])
            except: pass
        return False

    async def _create_session(self, proxy: str = None) -> str:
        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {
            "cmd": "sessions.create",
            "maxTimeout": (FLARESOLVERR_TIMEOUT + 60) * 1000,
        }
        if proxy:
            # FlareSolverr/Chromium preferisce socks5:// invece di socks5h://
            solver_proxy = proxy.replace("socks5h://", "socks5://") if proxy.startswith("socks5h://") else proxy
            payload["proxy"] = {"url": solver_proxy}
            
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    endpoint, 
                    json=payload, 
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "ok":
                            return data.get("session")
            except Exception as e:
                logger.error(f"FlareSolverr: Errore creazione sessione: {e}")
        return None

    async def release_session(self, session_id: str, is_persistent: bool):
        """Chiude la sessione se non è persistente."""
        if not session_id or is_persistent or not FLARESOLVERR_URL:
            return
            
        endpoint = f"{FLARESOLVERR_URL.rstrip('/')}/v1"
        payload = {"cmd": "sessions.destroy", "session": session_id}
        async with aiohttp.ClientSession() as session:
            try:
                await session.post(endpoint, json=payload, timeout=10)
            except Exception:
                pass

    async def report_invalid(self, session_id: str):
        """Invalida la sessione persistente se riscontra errori."""
        if not session_id:
            return
        async with self._lock:
            if session_id == self._warm_session_id:
                logger.warning(f"FlareSolverr: Sessione persistente {session_id} non valida. Rimozione.")
                self._warm_session_id = None
                # Tentiamo di distruggerla sul server per sicurezza
                await self.release_session(session_id, False)

# Singleton
solver_manager = SolverSessionManager()
