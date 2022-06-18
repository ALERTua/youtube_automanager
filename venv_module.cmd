
@echo off
set PYTHONIOENCODING=UTF-8
if defined verbose (
    set _verbose_venv=-i
)
venv.cmd %_verbose_venv% -m %*
