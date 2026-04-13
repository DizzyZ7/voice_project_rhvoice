from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from app.commands import runtime as service
from app.core.speech import RHVoiceTTS, VoskRecognizer, run_diagnostics, setup_logger

logger = setup_logger("voice_gui", "voice_gui.log")


class VoiceCommandGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Голосовой сервис | Vosk + RHVoice")
        self.root.geometry("700x420")
        self.queue: queue.Queue[str] = queue.Queue()
        self.stop_event = threading.Event()
        self.listener_thread: threading.Thread | None = None

        try:
            self.recognizer = VoskRecognizer(logger=logger)
            self.tts = RHVoiceTTS(logger=logger)
        except Exception as exc:
            messagebox.showerror("Ошибка запуска", str(exc))
            raise

        self._build_ui()
        self.refresh_diagnostics()
        self.root.after(200, self._poll_queue)

    def _build_ui(self):
        title = ttk.Label(self.root, text="Офлайн голосовой сервис", font=("Arial", 16, "bold"))
        title.pack(pady=12)

        self.status_var = tk.StringVar(value="Нажмите «Старт», чтобы начать слушать")
        ttk.Label(self.root, textvariable=self.status_var, wraplength=650).pack(pady=4)

        self.diag_var = tk.StringVar(value="Диагностика ещё не выполнена")
        ttk.Label(self.root, textvariable=self.diag_var, foreground="#333").pack(pady=4)

        buttons = ttk.Frame(self.root)
        buttons.pack(pady=10)
        self.start_btn = ttk.Button(buttons, text="Старт", command=self.start_listening)
        self.start_btn.grid(row=0, column=0, padx=6)
        self.stop_btn = ttk.Button(buttons, text="Стоп", command=self.stop_listening, state=tk.DISABLED)
        self.stop_btn.grid(row=0, column=1, padx=6)
        ttk.Button(buttons, text="Проверка окружения", command=self.refresh_diagnostics).grid(row=0, column=2, padx=6)

        commands_frame = ttk.LabelFrame(self.root, text="Ручные команды")
        commands_frame.pack(fill=tk.X, padx=12, pady=10)
        ttk.Button(commands_frame, text="Включить свет", command=self.manual_turn_on).pack(fill=tk.X, pady=4, padx=8)
        ttk.Button(commands_frame, text="Выключить свет", command=self.manual_turn_off).pack(fill=tk.X, pady=4, padx=8)
        ttk.Button(commands_frame, text="Температура", command=self.manual_temperature).pack(fill=tk.X, pady=4, padx=8)

        self.log_box = tk.Text(self.root, height=10, state=tk.DISABLED)
        self.log_box.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

    def append_log(self, text: str):
        self.log_box.config(state=tk.NORMAL)
        self.log_box.insert(tk.END, text + "\n")
        self.log_box.see(tk.END)
        self.log_box.config(state=tk.DISABLED)

    def refresh_diagnostics(self):
        diag = run_diagnostics()
        msg = (
            f"Vosk model: {'OK' if diag.vosk_model_exists else 'НЕТ'} | "
            f"RHVoice: {(diag.rhvoice_backend + ':' + (diag.rhvoice_target or '-')) if diag.rhvoice_available else 'не найден'} | "
            f"sounddevice: {'OK' if diag.sounddevice_available else 'не установлен'}"
        )
        self.diag_var.set(msg)
        self.append_log("[DIAG] " + msg)
        logger.info(msg)

    def start_listening(self):
        if self.listener_thread and self.listener_thread.is_alive():
            return
        self.stop_event.clear()
        self.listener_thread = threading.Thread(target=self._recognition_loop, daemon=True)
        self.listener_thread.start()
        self._set_listening_state(is_listening=True, status="Слушаю команды...")
        self.append_log("[INFO] Запущено прослушивание")

    def stop_listening(self):
        self.stop_event.set()
        self._set_listening_state(is_listening=False, status="Прослушивание остановлено")
        self.append_log("[INFO] Прослушивание остановлено")

    def _set_listening_state(self, is_listening: bool, status: str):
        self.status_var.set(status)
        self.start_btn.config(state=tk.DISABLED if is_listening else tk.NORMAL)
        self.stop_btn.config(state=tk.NORMAL if is_listening else tk.DISABLED)

    def _recognition_loop(self):
        while not self.stop_event.is_set():
            result = self.recognizer.transcribe_from_microphone(timeout=4)
            if not result.success:
                self.queue.put(f"Ошибка STT: {result.error}")
                continue
            if result.text:
                self.queue.put(f"Распознано: {result.text}")
                cont = service.parse_and_execute(result.text, self.tts)
                if not cont:
                    self.stop_event.set()
                    self.queue.put("Получена голосовая команда остановки")
                    break

    def _poll_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg == "Получена голосовая команда остановки":
                    self._set_listening_state(is_listening=False, status=msg)
                else:
                    self.status_var.set(msg)
                self.append_log(msg)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_queue)

    def manual_turn_on(self):
        service.turn_on_light()
        self.tts.speak("Готово")
        self.append_log("[MANUAL] Включить свет")

    def manual_turn_off(self):
        service.turn_off_light()
        self.tts.speak("Готово")
        self.append_log("[MANUAL] Выключить свет")

    def manual_temperature(self):
        service.get_temperature(self.tts)
        self.append_log("[MANUAL] Температура")


def main():
    root = tk.Tk()
    VoiceCommandGUI(root)
    root.mainloop()
