@echo off
set REGISTRY_IP=192.168.1.3
set REGISTRY_PORT=5001
set IMAGE_NAME=youtube_automanager
set IMAGE_TAG=latest
set BUILD_TAG=%REGISTRY_IP%:%REGISTRY_PORT%/%IMAGE_NAME%:%IMAGE_TAG%
set BUILD_PATH=.

set DOCKER_SERVICE=com.docker.service
set DOCKER_EXE=docker
where %DOCKER_EXE% >nul || (
    set "DOCKER_EXE=%ProgramFiles%\Docker\Docker\resources\bin\docker.exe"
)

pushd %~dp0
sc query %DOCKER_SERVICE% | findstr /IC:"running" >nul || (
    sudo net start %DOCKER_SERVICE% || (
        echo "Error starting docker service %DOCKER_SERVICE%
        exit /b
    )
)

tasklist | findstr /IC:"Docker Desktop.exe" >nul || (
    start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
    timeout /t 60
)

"%DOCKER_EXE%" build -t %BUILD_TAG% %BUILD_PATH% || exit /b
"%DOCKER_EXE%" push %REGISTRY_IP%:%REGISTRY_PORT%/%IMAGE_NAME% || exit /b

@REM net stop %DOCKER_SERVICE% || exit /b
