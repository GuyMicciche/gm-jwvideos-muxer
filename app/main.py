import os
import zipfile
import requests
import gzip
import io
import json
import subprocess
import shutil
import tempfile
import uuid
import re
import subprocess as sp
from flask import Flask, jsonify, render_template, request, send_from_directory, redirect, url_for, flash, send_file
from flask_bootstrap import Bootstrap5
from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import DataRequired
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient
from subtitle_processor import SubtitleProcessor
from pymkv import MKVFile, MKVTrack
from io import BytesIO
from pathlib import Path
from dotenv import load_dotenv

app = Flask(__name__)
app.secret_key = 'supersecretkey'  # Necessary for CSRF protection in Flask-WTF
Bootstrap5(app)

ROOT = Path(__file__).parent
os.environ["PATH"] += os.pathsep + str(ROOT / "bin")

APP_MODE = os.getenv('APP_MODE', 'DEBUG')

if APP_MODE == 'RELEASE':
    pass
else:
    load_dotenv()

# Azure Blob Storage setup (replace with your credentials)
AZURE_CONNECTION_STRING  = os.getenv('BLOB_CONNECTION_STRING')
AZURE_CONTAINER_NAME  = "convertedfiles"
blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
container_client = blob_service_client.get_container_client(AZURE_CONTAINER_NAME)

class NameForm(FlaskForm):
    name = StringField('Title', validators=[DataRequired()])

@app.route('/', methods=['GET', 'POST'])
def index():
    form = NameForm()
    if form.validate_on_submit():
        name = form.name.data
        return redirect(url_for('hello', name=name))
    return render_template('index.html', form=form)

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

# https://jwmediaconverter.azurewebsites.net/hello?name=%22Guy%22
@app.route('/hello')
def hello():
    name = request.args.get('name')
    if name:
        return render_template('hello.html', name=name)
    else:
        return redirect(url_for('index'))
    
@app.route('/search')
def search_titles():
    gz_url = 'https://app.jw-cdn.org/catalogs/media/E.json.gz'

    try:
        json_data = fetch_and_decompress_gz(gz_url)
        videos = [{"title": o['title'], "data": o} for o in json_data if 'title' in o]  # Extract full object but show title
        return jsonify(videos)
    except Exception as e:
        print(f"An error occurred: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/download', methods=['POST'])
def download_selected_videos():
    print(request)
      # Get the selected videos from the form data
    selected_videos_json = request.form.get('selected_videos', '[]')
    selected_videos = json.loads(selected_videos_json)
    
    combined_streams = []

    # Fetch, combine, and zip videos
    for video in selected_videos:
        try:
            language_agnostic_natural_key = video['data']['languageAgnosticNaturalKey']
            video_info = fetch_download_links(language_agnostic_natural_key)

            # Convert and combine video, audio, and subtitles
            #combined_stream = process_convert(video_info)
            combined_stream = combine_streams(video_info)
            if combined_stream:
                combined_streams.append({f"{video['data']['title']}.mkv": combined_stream})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    # Zip the combined streams
    zip_stream = create_zip(combined_streams)

    # Upload ZIP to Azure Blob Storage
    zip_blob_name = f"{str(uuid.uuid4())}.zip"
    try:
        zip_blob_url = upload_to_azure(zip_blob_name, zip_stream)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

    # Redirect to download page
    return redirect(url_for('download_page', download_url=zip_blob_url))

# Function to render a download page with the download URL
@app.route('/download_page')
def download_page():
    download_url = request.args.get('download_url')
    if download_url:
        return render_template('download.html', download_url=download_url)
    else:
        return "No download URL found.", 400

def fetch_and_decompress_gz(gz_url):
    response = requests.get(gz_url, stream=True)
    if response.status_code == 200:
        gz_data = io.BytesIO(response.content)
        with gzip.GzipFile(fileobj=gz_data, mode='rb') as f:
            extracted_titles = []
            for line in f:
                try:
                    json_obj = json.loads(line.decode('utf-8'))
                    o = json_obj.get('o', {})
                    if json_obj['type'] == 'media-item' and o.get('keyParts', {}).get('formatCode') == 'VIDEO':
                        extracted_titles.append(o)
                except json.JSONDecodeError:
                    continue
            return extracted_titles
    else:
        raise Exception(f"Failed to download gz file from {gz_url}")
    
# Function to create a ZIP file containing all the converted audio files in memory
def create_zip(media_streams):
    zip_stream = BytesIO()
    with zipfile.ZipFile(zip_stream, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for media_dict in media_streams:
            for file_name, media_stream in media_dict.items():  # Unpack the dictionary
                media_stream.seek(0)  # Reset the position of each audio stream
                zip_file.writestr(file_name, media_stream.read())  # Use the file name from the dictionary
    zip_stream.seek(0)  # Reset stream position to the beginning of the zip
    return zip_stream  # Return the in-memory zip stream

def upload_to_azure(blob_name, zip_buffer):
    blob_client = container_client.get_blob_client(blob_name)
    # Upload the audio stream directly to Azure Blob Storage
    blob_client.upload_blob(zip_buffer, blob_type="BlockBlob", overwrite=True)
    return blob_client.url  # Return the URL of the uploaded blob

def fetch_download_links(language_agnostic_natural_key):
    """
    Fetch the download links for both the English and Chinese Simplified versions of the video.
    Returns the largest video for both languages along with subtitle URLs if available.
    """
    def get_largest_file(media_data):
        return max(media_data['files'], key=lambda f: f['filesize'])
    
    base_url = "https://b.jw-cdn.org/apis/mediator/v1/media-items"

    # Fetch English media
    url_en = f"{base_url}/E/{language_agnostic_natural_key}?clientType=www"
    response_en = requests.get(url_en)
    
    # Fetch Chinese Simplified media
    url_chs = f"{base_url}/CHS/{language_agnostic_natural_key}?clientType=www"
    response_chs = requests.get(url_chs)
    
    if response_en.status_code == 200 and response_chs.status_code == 200:
        media_data_en = response_en.json().get('media', [])[0]
        media_data_chs = response_chs.json().get('media', [])[0]

        # Extract the largest video/audio file for each language
        largest_en = get_largest_file(media_data_en)
        largest_chs = get_largest_file(media_data_chs)

        # Fetch subtitles if available
        sub_e_url = largest_en.get('subtitles', {}).get('url')
        subtitles_en = sub_e_url if sub_e_url else 'None'
        sub_chs_url = largest_chs.get('subtitles', {}).get('url')
        subtitles_chs = sub_chs_url if sub_chs_url else 'None'

        return {
            "en": {
                "video_url": str(largest_en['progressiveDownloadURL']),
                "subtitles_url": str(subtitles_en),
                "title": str(media_data_en['title'])
            },
            "chs": {
                "video_url": str(largest_chs['progressiveDownloadURL']),
                "subtitles_url": str(subtitles_chs),
                "title": str(media_data_chs['title'])
            }
        }
    else:
        raise Exception("Failed to retrieve download links for one or both languages.")

def download_file(url):
    """Download the content from a URL and return it as a BytesIO object."""
    response = requests.get(url, stream=True)
    if response.status_code == 200:
        stream = BytesIO()
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                stream.write(chunk)
        stream.seek(0)  # Reset the stream position to the beginning

        return stream
    else:
        raise Exception(f"Failed to download file from {url}")

# Function to download video and audio and write to a temporary file
def download_to_tempfile(url):
    response = requests.get(url)

    if response.status_code == 200:
        # Extract the file extension from the URL   

        extension = os.path.splitext(url)[1]

        # Create a temporary file with the appropriate extension
        with tempfile.NamedTemporaryFile(delete=False, suffix=extension) as temp_file:
            temp_file.write(response.content)

            return temp_file.name  # Return the path of the temporary file
    else:
        raise Exception(f"Failed to download from {url}")
    
# Function to convert VTT to SRT and return the content as a BytesIO object
def convert_vtt_to_temp_srt(vtt_filepath):
    # Create a temporary SRT file with automatic deletion
    with tempfile.NamedTemporaryFile(delete=False, suffix=".vtt") as temp_srt:
        # Open and read the VTT file
        with open(vtt_filepath, 'r', encoding='utf-8') as vtt_file:
            vtt_content = vtt_file.read()

        # Remove the WEBVTT header
        srt_content = re.sub(r'WEBVTT\s*\n', '', vtt_content)

        # Replace VTT cue timing format (00:00:00.000 --> 00:00:00.000) with SRT format (00:00:00,000 --> 00:00:00,000)
        srt_content = re.sub(r'(\d+:\d+:\d+)\.(\d+)', r'\1,\2', srt_content)

        # Remove formatting attributes like line, position, align
        srt_content = re.sub(r'(\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}).*', r'\1', srt_content)

        # Add SRT sequence numbers
        srt_blocks = srt_content.strip().split('\n\n')
        srt_content_with_numbers = ""
        for i, block in enumerate(srt_blocks):
            srt_content_with_numbers += f"{i + 1}\n{block.strip()}\n\n"

        # Write the final SRT content to the temporary file
        temp_srt.write(srt_content_with_numbers.encode('utf-8'))
        temp_srt.flush()  # Ensure the content is written

        # Rewind and read the content into a BytesIO object
        temp_srt.seek(0)
        srt_bytes = io.BytesIO(temp_srt.read())
        
    os.remove(temp_srt.name)

    return srt_bytes  # Return the bytes from the BytesIO object

    # The temporary file is deleted when the block exits, but the BytesIO stream retains the content
    print(f"Converted {vtt_filepath} to SRT and delivered as BytesIO")

def combine_streams(video_info):
    """
    Combine English and Chinese videos and subtitles into a single file.
    """
    try:
        english_title = video_info['en']['title']
        chinese_title = video_info['chs']['title']
        video_en_url = video_info['en']['video_url']
        video_chs_url = video_info['chs']['video_url']
        subtitles_en_url = video_info['en']['subtitles_url'] if video_info['en']['subtitles_url'] != 'None' else None
        subtitles_chs_url = video_info['chs']['subtitles_url'] if video_info['chs']['subtitles_url'] != 'None' else None

        # Combine the audio and subtitles using ffmpeg
        #combined_stream = do_ffmpeg(english_title, chinese_title, video_en_url, video_chs_url, subtitles_en_url, subtitles_chs_url)
        combined_stream = do_mkvmerge(english_title, chinese_title, video_en_url, video_chs_url, subtitles_en_url, subtitles_chs_url)

        return combined_stream
    except Exception as e:
        print(f"Error during conversion: {e}")
        return None
    
def do_mkvmerge(english_title, chinese_title, video_en, video_chs, subtitles_en=None, subtitles_chs=None):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_video_en, \
             tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as temp_video_chs, \
             tempfile.NamedTemporaryFile(delete=False, suffix=".vtt") as temp_subtitles_en, \
             tempfile.NamedTemporaryFile(delete=False, suffix=".srt") as temp_srt_en, \
             tempfile.NamedTemporaryFile(delete=False, suffix=".vtt") as temp_subtitles_chs, \
             tempfile.NamedTemporaryFile(delete=False, suffix=".srt") as temp_srt_chs, \
             tempfile.NamedTemporaryFile(delete=False, suffix=".srt") as temp_srt_pinyin:
            
            temp_video_en.write(download_file(video_en).read())
            temp_video_en.seek(0)  # Rewind

            temp_video_chs.write(download_file(video_chs).read())
            temp_video_chs.seek(0)  # Rewind
            
            if subtitles_en:
                temp_subtitles_en.write(download_file(subtitles_en).read())
                temp_subtitles_en.seek(0)  # Rewind
                temp_srt_en.write(convert_vtt_to_temp_srt(temp_subtitles_en.name).read())
                temp_srt_en.seek(0)  # Rewind
            if subtitles_chs:
                temp_subtitles_chs.write(download_file(subtitles_chs).read())
                temp_subtitles_chs.seek(0)  # Rewind
                temp_srt_chs.write(convert_vtt_to_temp_srt(temp_subtitles_chs.name).read())
                temp_srt_chs.seek(0)  # Rewind
                processor = SubtitleProcessor()
                processor.generate_pinyin_subtitle_file(temp_srt_chs.name, temp_srt_pinyin.name)
                temp_srt_pinyin.seek(0)  # Rewind
        
            # Create an MKVFile object
            mkv = MKVFile()

            # Add the English video and audio tracks
            mkv.add_track(MKVTrack(temp_video_en.name, track_id=0))
            mkv.add_track(MKVTrack(temp_video_en.name, track_id=1, language="eng", track_name=english_title))

            # Add the Chinese audio track
            mkv.add_track(MKVTrack(temp_video_chs.name, track_id=1, language="chi", track_name=chinese_title))

            # Add subtitle tracks if available
            if subtitles_en:
                mkv.add_track(MKVTrack(temp_srt_en.name, language="eng", track_name="English"))

            if subtitles_chs:
                mkv.add_track(MKVTrack(temp_srt_chs.name, language="chi", track_name="中文"))
                mkv.add_track(MKVTrack(temp_srt_pinyin.name, language="chi", track_name="Pīnyīn"))

            temp_mkv_file = os.path.join(ROOT.parent, "output.mkv")
            mkv.mux(temp_mkv_file)

            with open(temp_mkv_file, 'rb') as f:
                stream = io.BytesIO(f.read())
                            
            print("MKV file created successfully.")
            return stream
    except Exception as e:
        # Delete the temporary files
        os.remove(temp_mkv_file)
        for temp_file in [temp_video_en, temp_video_chs, temp_subtitles_en, 
                            temp_srt_en, temp_subtitles_chs, temp_srt_chs, temp_srt_pinyin]:
            temp_file.close()
            os.remove(temp_file.name)
        print(f"An error occurred: {e}")
        raise
    finally:
        # Delete the temporary files
        os.remove(temp_mkv_file)
        for temp_file in [temp_video_en, temp_video_chs, temp_subtitles_en, 
                            temp_srt_en, temp_subtitles_chs, temp_srt_chs, temp_srt_pinyin]:
            temp_file.close()
            os.remove(temp_file.name)

def do_ffmpeg(english_title, chinese_title, video_en_url, video_chs_url, subtitles_en_url=None, subtitles_chs_url=None):
    """
    Combine English and Chinese audio streams and subtitles into a single MKV file.
    Returns the MKV file as a file stream.
    """
    # Download English video
    video_en = download_file(video_en_url)
    # Download Chinese video
    video_chs = download_file(video_chs_url)

    if video_en is None or video_chs is None:
        raise ValueError("Video streams cannot be None")

    # Download subtitles (if available)
    if subtitles_en_url is not None:
        subtitles_en = download_file(subtitles_en_url)
    if subtitles_chs_url is not None:
        subtitles_chs = download_file(subtitles_chs_url)
    
    output_stream = BytesIO()

    with tempfile.NamedTemporaryFile(delete=False) as temp_video_en, \
         tempfile.NamedTemporaryFile(delete=False) as temp_video_chs:
        
        # Write video streams to temporary files
        temp_video_en.write(video_en.getvalue())
        temp_video_chs.write(video_chs.getvalue())

        command = [
            'ffmpeg', '-y',  # Overwrite output file if exists
            '-i', temp_video_en.name,  # English video input (temp file)
            '-i', temp_video_chs.name  # Chinese video input (temp file)
        ]

        # Add subtitles input commands
        if subtitles_en:
            with tempfile.NamedTemporaryFile(delete=False) as temp_subtitles_en:
                temp_subtitles_en.write(subtitles_en.getvalue())
                command.extend(['-i', temp_subtitles_en.name])  # English subtitle input

        if subtitles_chs:
            with tempfile.NamedTemporaryFile(delete=False) as temp_subtitles_chs:
                temp_subtitles_chs.write(subtitles_chs.getvalue())
                command.extend(['-i', temp_subtitles_chs.name])  # Chinese subtitle input

        # Map the audio and video streams
        command.extend([
            '-map_metadata', '0',
            '-c:v', 'copy', '-c:a', 'copy',  # Copy video and audio codecs, no transcoding
            '-map', '0:v:0',  # Map the video stream from the primary file
            '-map', '0:a:0',  # Map the audio stream from the primary file
            '-map', '1:a:0',  # Map Chinese audio
        ])

        # Add subtitle streams if available
        if subtitles_en:
            command.extend(['-map', '2:s:0', '-c:s:0', 'srt'])  # Map and set codec for English subtitles
        if subtitles_chs:
            command.extend(['-map', '3:s:0', '-c:s:1', 'srt'])  # Map and set codec for Chinese subtitles

        # Add metadata for audio streams
        command.extend([
            '-metadata:s:a:0', f'title={english_title}', '-metadata:s:a:0', 'language=eng',  # Metadata for English audio
            '-metadata:s:a:1', f'title={chinese_title}', '-metadata:s:a:1', 'language=chi',  # Metadata for Chinese audio
        ])

        # Add metadata for subtitle streams
        if subtitles_en:
            command.extend(['-metadata:s:s:0', 'language=eng', '-metadata:s:s:0', 'title=English'])  # Metadata for English subtitles
        if subtitles_chs:
            command.extend(['-metadata:s:s:1', 'language=chi', '-metadata:s:s:1', 'title=中文'])  # Metadata for Chinese subtitles

        command.extend(['-f', 'matroska', 'pipe:1'])  # Output as MKV format to stdout

        # Run the ffmpeg process and capture output
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        try:
            # Read the stdout (output MKV stream) and write it to output_stream
            output, error = process.communicate()

            if process.returncode != 0:
                print(f"FFmpeg error: {error.decode('utf-8')}")
                raise Exception("Error during FFmpeg processing")

            output_stream.write(output)
            output_stream.seek(0)  # Reset stream pointer for reading
            return output_stream

        finally:
            # Clean up the temporary filess
            temp_files = [temp_video_en, temp_video_chs, temp_subtitles_en, temp_subtitles_chs]
            for temp_file in temp_files:
                try:
                    os.remove(temp_file.name)
                except OSError:
                    pass
      
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)