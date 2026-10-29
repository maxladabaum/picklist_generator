import json


def test_existing_config_updates_once_and_keeps_later_user_choice(tmp_path, monkeypatch):
    import picklist_app
    for name in ('CONFIG_PATH', 'PLATE_STATE_PATH', 'STORAGE_STATE_PATH'):
        monkeypatch.setattr(picklist_app, name, tmp_path / (name + '.json'))
    config_path = picklist_app.CONFIG_PATH
    config_path.write_text(json.dumps({'transfer_volume_nL': 75, 'unrelated_setting': 'keep'}))
    app = picklist_app.PicklistApp()
    app.withdraw()
    try:
        assert app.transfer_volume.get() == '25'
        assert all(view.base_transfer_volume.get() == '25' for view in app.set_views)
        saved = json.loads(config_path.read_text())
        assert saved['transfer_volume_nL'] == 25
        assert saved['unrelated_setting'] == 'keep'
        app.transfer_volume.set('100')
        app._flush_config()
    finally:
        app.destroy()
    reopened = picklist_app.PicklistApp()
    reopened.withdraw()
    try:
        assert reopened.transfer_volume.get() == '100'
    finally:
        reopened.destroy()
