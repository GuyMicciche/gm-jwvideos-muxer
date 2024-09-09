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
from flask import Flask, jsonify, render_template, request, send_from_directory, redirect, url_for, flash, send_file
from flask_bootstrap import Bootstrap5
from flask_wtf import FlaskForm
from wtforms import StringField
from wtforms.validators import DataRequired
from azure.storage.blob import BlobServiceClient
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
    ffmpeg_path = "/home/site/wwwroot/bin/ffmpeg" # not used, ffmpeg install included in the yml
else:
    # Assuming ffmpeg is located at a different path during debugging
    ffmpeg_path = 'ffmpeg' # not used, ffmpeg in bin app/bin directory
    load_dotenv()

# Azure Blob Storage setup (replace with your credentials)
BLOB_CONNECTION_STRING = os.getenv('BLOB_CONNECTION_STRING')
BLOB_CONTAINER_NAME = "convertedfiles"
blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)

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

            print(video_info)

            # Convert and combine video, audio, and subtitles
            combined_stream = process_convert(video_info)
            if combined_stream:
                combined_streams.append({f"{video['data']['title']}.mkv": combined_stream})
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    # Zip the combined streams
    zip_stream = create_zip_in_memory(combined_streams)

    # Upload ZIP to Azure Blob Storage
    zip_blob_name = f"{str(uuid.uuid4())}.zip"
    try:
        zip_blob_url = upload_to_blob_from_memory(zip_stream, zip_blob_name)
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
    
# Function to fetch video content as an in-memory stream
def download_video(video_url):
    response = requests.get(video_url, stream=True)
    video_stream = BytesIO()
    for chunk in response.iter_content(chunk_size=8192):
        if chunk:
            video_stream.write(chunk)
    video_stream.seek(0)  # Reset the stream position to the beginning
    return video_stream  # Return video stream in memory

# Function to create a ZIP file containing all the converted audio files in memory
def create_zip_in_memory(media_streams):
    zip_stream = BytesIO()
    with zipfile.ZipFile(zip_stream, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for media_dict in media_streams:
            for file_name, media_stream in media_dict.items():  # Unpack the dictionary
                media_stream.seek(0)  # Reset the position of each audio stream
                zipf.writestr(file_name, media_stream.read())  # Use the file name from the dictionary
    zip_stream.seek(0)  # Reset stream position to the beginning of the zip
    return zip_stream  # Return the in-memory zip stream

def upload_to_blob_from_memory(audio_stream, blob_name):
    blob_client = container_client.get_blob_client(blob_name)
    # Upload the audio stream directly to Azure Blob Storage
    blob_client.upload_blob(audio_stream, blob_type="BlockBlob", overwrite=True)
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

def process_convert(video_info):
    """
    Combine English and Chinese videos and subtitles into a single file.
    """
    try:
        # Download English video
        video_en = download_video(video_info['en']['video_url'])
        # Download Chinese video
        video_chs = download_video(video_info['chs']['video_url'])

        if video_en is None or video_chs is None:
            raise ValueError("Video streams cannot be None")

        # Download subtitles (if available)
        subtitles_en = download_subtitles(video_info['en']['subtitles_url']) if video_info['en']['subtitles_url'] != 'None' else None
        subtitles_chs = download_subtitles(video_info['chs']['subtitles_url']) if video_info['chs']['subtitles_url'] != 'None' else None

        # Combine the audio and subtitles using ffmpeg
        combined_stream = do_mux(video_en, video_chs, subtitles_en, subtitles_chs)

        return combined_stream
    except Exception as e:
        print(f"Error during conversion: {e}")
        return None

def do_mux(video_en, video_chs, subtitles_en=None, subtitles_chs=None):
    """
    Combine English and Chinese audio streams and subtitles into a single MKV file.
    Returns the MKV file as a file stream.
    """
    output_stream = BytesIO()

    with tempfile.NamedTemporaryFile(delete=False) as temp_video_en, \
         tempfile.NamedTemporaryFile(delete=False) as temp_video_chs:
        
        temp_files = [temp_video_en.name, temp_video_chs.name]

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
                temp_files.append(temp_subtitles_en.name)
                command.extend(['-i', temp_subtitles_en.name])  # English subtitle input

        if subtitles_chs:
            with tempfile.NamedTemporaryFile(delete=False) as temp_subtitles_chs:
                temp_subtitles_chs.write(subtitles_chs.getvalue())
                temp_files.append(temp_subtitles_chs.name)
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
            '-metadata:s:a:0', 'title=English', '-metadata:s:a:0', 'language=eng',  # Metadata for English audio
            '-metadata:s:a:1', 'title=Chinese', '-metadata:s:a:1', 'language=chi',  # Metadata for Chinese audio
        ])

        # Add metadata for subtitle streams
        if subtitles_en:
            command.extend(['-metadata:s:s:0', 'language=eng', '-metadata:s:s:0', 'title=English'])  # Metadata for English subtitles
        if subtitles_chs:
            command.extend(['-metadata:s:s:1', 'language=chi', '-metadata:s:s:1', 'title=Chinese'])  # Metadata for Chinese subtitles

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
            # Clean up the temporary files
            for temp_file in temp_files:
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
      
def download_subtitles(subtitle_url):
    if not subtitle_url:
        return None
    """
    Download subtitles and return the file path.
    """
    response = requests.get(subtitle_url, stream=True)
    subtitle_stream = BytesIO()

    if response.status_code == 200:
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                subtitle_stream.write(chunk)
        subtitle_stream.seek(0)  # Reset the stream position
        return subtitle_stream
    else:
        raise Exception(f"Failed to download subtitles from {subtitle_url}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)