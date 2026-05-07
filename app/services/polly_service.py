import boto3
import os
from contextlib import closing

class PollyService:
    def __init__(self):
        self.polly = boto3.client(
            "polly",
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_REGION_NAME", "us-east-1")
        )
        # Voz 'Mia' es Neural, femenina y de México (muy fluida)
        self.voice_id = "Mia" 
        self.engine = "neural"

    async def text_to_speech(self, text: str, output_path: str = "/tmp/response.mp3"):
        try:
            response = self.polly.synthesize_speech(
                Text=text,
                OutputFormat="mp3",
                VoiceId=self.voice_id,
                Engine=self.engine
            )

            if "AudioStream" in response:
                with closing(response["AudioStream"]) as stream:
                    with open(output_path, "wb") as file:
                        file.write(stream.read())
                return output_path
        except Exception as e:
            print(f"Error en Polly: {e}")
            return None

polly_service = PollyService()