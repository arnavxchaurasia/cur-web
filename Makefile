.PHONY: dev build build-linux test deploy-backend deploy-frontend

dev:
	go run ./cmd/server

build:
	go build -o cur-web ./cmd/server

build-linux:
	GOOS=linux GOARCH=amd64 go build -o cur-web-linux ./cmd/server

test:
	go test ./...

deploy-backend: build-linux
	scp cur-web-linux $(VM_USER)@$(VM_HOST):/usr/local/bin/cur-web
	ssh $(VM_USER)@$(VM_HOST) "sudo systemctl restart cur-web"

deploy-frontend:
	cd frontend && npm run build
	rsync -av --delete frontend/dist/ $(VM_USER)@$(VM_HOST):/var/www/cur-web/
