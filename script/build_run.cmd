
@if not defined DOCKER_HOST set DOCKER_HOST=tcp://docker:2375
@if not defined STAGE set STAGE=production
@if not defined CONTAINER_NAME set CONTAINER_NAME=test
@if not defined DOCKERFILENAME set DOCKERFILENAME=Dockerfile

docker kill %CONTAINER_NAME%
docker rm %CONTAINER_NAME%
docker build -f %DOCKERFILENAME% --target %STAGE% -t %CONTAINER_NAME%:latest . || exit /b 1
docker run --init --env-file="%~dp0..\.env" --name %CONTAINER_NAME% -t -d %CONTAINER_NAME% || exit /b 1
where nircmd >nul 2>nul && nircmd beep 500 500
docker logs %CONTAINER_NAME%
docker exec -it %CONTAINER_NAME% bash
