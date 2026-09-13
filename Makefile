SERVICE := spotify_screensaver.service
RESUME_SERVICE := spotify-resume-fix.service
UNIT_DIR := $(HOME)/.config/systemd/user

.PHONY: install run status logs install-resume resume-logs

install:
	mkdir -p $(UNIT_DIR)
	cp $(SERVICE) $(UNIT_DIR)/$(SERVICE)
	systemctl --user daemon-reload
	systemctl --user enable $(SERVICE)
	systemctl --user restart $(SERVICE)

install-resume:
	mkdir -p $(UNIT_DIR)
	cp $(RESUME_SERVICE) $(UNIT_DIR)/$(RESUME_SERVICE)
	systemctl --user daemon-reload
	systemctl --user enable $(RESUME_SERVICE)
	systemctl --user restart $(RESUME_SERVICE)

run:
	python3 spotify_screensaver.py 3

status:
	systemctl --user status $(SERVICE) --no-pager

logs:
	journalctl --user -u $(SERVICE) --no-pager -n 20

resume-logs:
	journalctl --user -u $(RESUME_SERVICE) --no-pager -n 20
