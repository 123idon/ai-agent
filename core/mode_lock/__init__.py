"""Mode-lock primitives.

- live 진입 시 config/strategy_params.yaml의 SHA-256 해시를 캡처하고
  운영 중 mtime/해시 변경을 감지하면 즉시 Kill Switch를 발동시킨다.
"""
