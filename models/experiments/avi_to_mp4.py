import os
from moviepy.editor import VideoFileClip

def convert_avi_to_mp4(input_file_path, output_file_path):
    """
    Converts a video file from AVI to MP4 format using moviepy.

    Args:
        input_file_path (str): The full path to the input .avi file.
        output_file_path (str): The full path where the .mp4 file should be saved.
    """
    if not os.path.exists(input_file_path):
        print(f"Error: Input file not found at {input_file_path}")
        return

    print(f"Loading clip: {input_file_path}")
    try:
        # Load the AVI file
        clip = VideoFileClip(input_file_path)

        # Write the clip to an MP4 file. 
        # 'libx264' is the standard video codec for MP4. 
        # 'aac' is the standard audio codec.
        print(f"Starting conversion to {output_file_path}...")
        clip.write_videofile(
            output_file_path,
            codec='libx264',
            audio_codec='aac',
            temp_audiofile='temp-audio.m4a', # Temporary file used during conversion
            remove_temp=True,                # Deletes the temporary file after conversion
            logger='bar'                     # Shows a progress bar
        )
        clip.close() # Close the clip after processing

        print(f"\nConversion successful! MP4 file saved as: {output_file_path}")

    except Exception as e:
        print(f"\nAn error occurred during conversion: {e}")
        # Clean up the temporary audio file if an error occurred before completion
        if os.path.exists('temp-audio.m4a'):
            os.remove('temp-audio.m4a')
            print("Cleaned up temporary audio file.")


if __name__ == "__main__":
    
    INPUT_AVI = 'annotated_output_parallel.avi' 
    
    OUTPUT_MP4 = 'annotated_output_parallel.mp4'

    # Run the conversion
    convert_avi_to_mp4(INPUT_AVI, OUTPUT_MP4)