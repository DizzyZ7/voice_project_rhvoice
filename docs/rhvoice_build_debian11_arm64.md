# Сборка RHVoice из исходников под Debian 11 (bullseye) на Raspberry Pi 4 (aarch64)

Эта инструкция нужна для случаев, когда готовые `deb`/`dpkg` пакеты RHVoice недоступны или не ставятся.

Проверено для:
- ОС: Debian GNU/Linux 11 (bullseye)
- Ядро: 6.1.21-v8+
- Платформа: Raspberry Pi 4 Model B
- Архитектура: `aarch64` (`arm64`)

## 1) Подготовка системы

```bash
sudo apt update
sudo apt install -y --no-install-recommends \
  git ca-certificates build-essential pkg-config scons \
  libpulse-dev libao-dev portaudio19-dev libspeechd-dev speech-dispatcher \
  libssl-dev zlib1g-dev
```

## 2) Сборка и установка RHVoice

```bash
git clone --recursive https://github.com/RHVoice/RHVoice.git
cd RHVoice
scons -j"$(nproc)"
sudo scons install prefix=/usr
sudo ldconfig
```

Проверка:

```bash
echo "проверка" | RHVoice-test
```

Если бинарь имеет другое имя:

```bash
echo "проверка" | rhvoice.test
```

## 3) Установка голоса

После сборки движка голоса могут отсутствовать. Если доступен встроенный менеджер:

```bash
sudo rhvoice.vm -a          # список доступных голосов
sudo rhvoice.vm -i anna     # пример установки русского голоса
sudo rhvoice.vm -l          # список установленных
```

## 4) Интеграция с этим проектом

В проекте используется переменная `RHVOICE_BIN`:

```bash
export RHVOICE_BIN=RHVoice-test
```

или

```bash
export RHVOICE_BIN=rhvoice.test
```

Далее можно запускать сервисы проекта как обычно.

## 5) Автоматизированный скрипт

В репозитории есть скрипт:

```bash
./scripts/install_rhvoice_linux_aarch64.sh
```

Пример с явным тегом RHVoice и установкой голоса:

```bash
./scripts/install_rhvoice_linux_aarch64.sh --version 1.18.4 --voice anna
```

## Источники

- RHVoice (официальный репозиторий): https://github.com/RHVoice/RHVoice
- RHVoice docs, Compiling on Linux: https://github.com/RHVoice/RHVoice/blob/master/doc/en/Compiling-on-Linux.md
- RHVoice docs, Packaging status: https://github.com/RHVoice/RHVoice/blob/master/doc/en/Packaging-status.md
