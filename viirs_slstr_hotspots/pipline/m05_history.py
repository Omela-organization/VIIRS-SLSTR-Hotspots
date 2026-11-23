

def run(cfg, inputs, outputs, journal, quality):
    for path in outputs.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"status": "stub"}')

    return {"ok": True}
