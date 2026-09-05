SERVICE := spotify_screensaver.service
UNIT_DIR := $(HOME)/.config/systemd/user

.PHONY: install run status logs

install:
	mkdir -p $(UNIT_DIR)
	cp $(SERVICE) $(UNIT_DIR)/$(SERVICE)
	systemctl --user daemon-reload
	systemctl --user enable $(SERVICE)
	systemctl --user restart $(SERVICE)

run:
	python3 spotify_screensaver.py 3

status:
	systemctl --user status $(SERVICE) --no-pager

logs:
	journalctl --user -u $(SERVICE) --no-pager -n 20
