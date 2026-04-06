from tests.test_stack_legacy import (
    test_command_routing,
    test_rhvoice_command_build,
    test_stt_service_streams_upload_to_disk,
    test_tts_path_is_confined_to_output_dir,
)


def main():
    print("=== TEST RHVOICE/VOSK STACK ===")
    test_rhvoice_command_build()
    test_command_routing()
    test_tts_path_is_confined_to_output_dir()
    test_stt_service_streams_upload_to_disk()
    print("=== DONE ===")


if __name__ == "__main__":
    main()
