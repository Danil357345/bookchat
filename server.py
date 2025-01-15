#!/usr/bin/env python3

import http.server
import socketserver
import json
import os
from pathlib import Path
import logging
import traceback
import re
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from http import HTTPStatus
from dotenv import load_dotenv
import threading
import time
import subprocess
from jinja2 import Environment, FileSystemLoader

from storage.file_storage import FileStorage

# Configure logging with a more detailed format and multiple levels
log_format = '%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
console_format = '%(asctime)s - %(levelname)s - %(message)s'  # Simpler format for console

# Create logs directory if it doesn't exist
os.makedirs('logs', exist_ok=True)

# Remove all existing handlers
root = logging.getLogger()
if root.handlers:
    for handler in root.handlers:
        root.removeHandler(handler)

# Configure file handlers for different log levels
debug_handler = logging.FileHandler('logs/debug.log')
debug_handler.setLevel(logging.DEBUG)
debug_handler.setFormatter(logging.Formatter(log_format))

info_handler = logging.FileHandler('logs/info.log')
info_handler.setLevel(logging.INFO)
info_handler.setFormatter(logging.Formatter(log_format))

error_handler = logging.FileHandler('logs/error.log')
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(logging.Formatter(log_format))

# Configure console handler with simpler format and ERROR level only
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.ERROR)  # Only show ERROR level messages in console
console_handler.setFormatter(logging.Formatter(console_format))

# Add all handlers to root logger
root.addHandler(debug_handler)
root.addHandler(info_handler)
root.addHandler(error_handler)
root.addHandler(console_handler)

# Set root logger to lowest level (DEBUG) to catch all logs
root.setLevel(logging.DEBUG)

# Create a logger specific to this application
logger = logging.getLogger('bookchat')

# Load environment variables
load_dotenv()

# Feature flags
MESSAGE_VERIFICATION_ENABLED = os.getenv('MESSAGE_VERIFICATION', 'false').lower() == 'true'

# Configuration
PORT = int(os.getenv('PORT', 8000))
REPO_PATH = os.getenv('REPO_PATH', os.path.abspath(os.path.dirname(__file__)))

# Initialize storage backend
logger.info(f"Initializing storage backend with repo path: {REPO_PATH}")
storage = FileStorage(REPO_PATH)
storage.init_storage()

class ChatRequestHandler(http.server.SimpleHTTPRequestHandler):
    """Custom request handler for the chat application"""
    
    def __init__(self, *args, **kwargs):
        # Set up Jinja2 environment
        self.jinja_env = Environment(loader=FileSystemLoader('templates'))
        # Set the directory for serving static files
        super().__init__(*args, directory="static", **kwargs)
    
    def handle_error(self, status_code: int, message: str) -> None:
        """Handle errors and return appropriate response."""
        try:
            # Log error with traceback
            logger.error(f"Error {status_code}: {message}\n{traceback.format_exc()}")
            
            # Don't send error response for broken pipe or connection reset
            if isinstance(message, (BrokenPipeError, ConnectionResetError)):
                logger.debug(f"Client disconnected: {message}")
                return
            
            # Send error response
            self.send_response(status_code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            
            error_response = {
                'error': {
                    'code': status_code,
                    'message': str(message)
                }
            }
            self.wfile.write(json.dumps(error_response).encode('utf-8'))
            
        except Exception as e:
            # Last resort error handling
            logger.error(f"Error in error handler: {e}")
    
    def do_GET(self):
        """Handle GET requests for various endpoints including static files, messages, and public keys"""
        try:
            parsed_path = urlparse(self.path)
            path = parsed_path.path
            
            client_address = self.client_address[0]
            logger.info(f"GET request from {client_address} to {path}")
            
            # Main application routes
            if path == '/':
                # Serve main page
                logger.debug("Serving main page")
                self.serve_file('templates/index.html', 'text/html')
            elif path == '/messages':
                # Serve messages
                logger.debug("Handling messages request")
                self.serve_messages()
            elif path == '/verify_username':
                # Verify username
                logger.debug("Handling username verification")
                self.verify_username()
            elif path == '/status':
                # Serve status page
                self.serve_status_page()
                
            # Public key handling
            elif path.startswith('/public_keys/'):
                # Serve public key
                key_name = path.split('/')[-1]
                key_path = Path('public_keys') / key_name
                if key_path.exists() and key_path.suffix == '.pub':
                    self.serve_file(str(key_path), 'text/plain')
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    
            # Individual message handling
            elif path.startswith('/messages/'):
                # Serve individual message
                filename = path.split('/')[-1]
                message_path = Path('messages') / filename
                if message_path.exists() and message_path.is_file():
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'text/plain')
                    self.end_headers()
                    self.wfile.write(message_path.read_bytes())
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "Message file not found")
                    
            # Static file handling
            elif path.startswith('/static/'):
                # Serve static file
                try:
                    file_path = path[8:]  # Remove '/static/' prefix
                    logger.debug(f"Serving static file: {file_path}")
                    with open(os.path.join('static', file_path), 'rb') as f:
                        content = f.read()
                    
                    self.send_response(HTTPStatus.OK)
                    # Basic content type detection
                    content_type = 'text/css' if file_path.endswith('.css') else 'application/javascript'
                    self.send_header('Content-Type', content_type)
                    self.send_header('Content-Length', str(len(content)))
                    
                    # Add caching headers
                    # Cache static assets for 1 week (604800 seconds)
                    self.send_header('Cache-Control', 'public, max-age=604800, immutable')
                    # Provide ETag for cache validation
                    etag = f'"{hash(content)}"'
                    self.send_header('ETag', etag)
                    # Add Vary header to handle different encodings
                    self.send_header('Vary', 'Accept-Encoding')
                    
                    self.end_headers()
                    self.wfile.write(content)
                    logger.debug(f"Successfully served static file: {file_path}")
                except FileNotFoundError:
                    logger.error(f"Static file not found: {file_path}")
                    self.send_error(HTTPStatus.NOT_FOUND)
                except Exception as e:
                    logger.error(f"Error serving file {file_path}: {e}")
                    self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
                    
            # Public keys directory handling
            elif path.startswith('/identity/public_keys/'):
                # Serve public key from identity directory
                username = path.split('/')[-1].split('.')[0]
                public_key_path = os.path.join(REPO_PATH, 'identity/public_keys', f'{username}.pub')
                if os.path.exists(public_key_path):
                    self.send_response(HTTPStatus.OK)
                    self.send_header('Content-Type', 'text/plain')
                    self.end_headers()
                    with open(public_key_path, 'r') as f:
                        self.wfile.write(f.read().encode('utf-8'))
                else:
                    self.send_error(HTTPStatus.NOT_FOUND, "Public key not found")
                    
            # Fallback to default handler
            else:
                # Attempt to serve unknown path
                logger.info(f"Attempting to serve unknown path: {path}")
                super().do_GET()
                
        except (BrokenPipeError, ConnectionResetError) as e:
            # Common client disconnect scenarios - log but don't treat as error
            logger.info(f"Client disconnected during response: {e}")
        except Exception as e:
            logger.error(f"Error in GET request handler", exc_info=True)
            self.handle_error(500, str(e))

    def do_POST(self):
        """Handle POST requests"""
        try:
            parsed_path = urlparse(self.path)
            client_address = self.client_address[0]
            logger.info(f"POST request from {client_address} to {parsed_path.path}")
            
            if parsed_path.path == '/messages':
                self.handle_message_post()
            elif parsed_path.path == '/username':
                self.handle_username_post()
            elif parsed_path.path == '/change_username':
                self.handle_username_change()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as e:
            logger.error(f"Error in POST request handler", exc_info=True)
            self.handle_error(500, str(e))

    def handle_message_post(self) -> None:
        """Handle message posting"""
        try:
            # Read and parse request body
            content_length = int(self.headers.get('Content-Length', 0))
            logger.debug(f"Content-Length: {content_length}")
            logger.debug(f"Content-Type: {self.headers.get('Content-Type', '')}")
            
            if content_length == 0:
                logger.error("No content received in request")
                self.handle_error(400, "No content received")
                return
            
            body = self.rfile.read(content_length).decode('utf-8')
            logger.debug(f"Raw request body: {body}")
            
            # Handle both JSON and form data
            content_type = self.headers.get('Content-Type', '')
            if 'application/json' in content_type:
                try:
                    data = json.loads(body)
                    logger.debug(f"Parsed JSON data: {data}")
                    content = data.get('content')
                    username = data.get('username', 'anonymous')
                except json.JSONDecodeError as e:
                    logger.error(f"Error decoding JSON: {e}")
                    self.handle_error(400, "Invalid JSON format")
                    return
            else:
                # Parse form data
                form_data = parse_qs(body)
                logger.debug(f"Parsed form data: {form_data}")
                content = form_data.get('content', [''])[0]
                username = form_data.get('username', ['anonymous'])[0]
            
            # Validate content after parsing
            if content is None:
                logger.error("Message content is None")
                self.handle_error(400, "Message content is required")
                return
            
            content = content.strip()
            if not content:
                logger.error("Message content is empty after stripping")
                self.handle_error(400, "Message content cannot be empty")
                return

            logger.debug(f"Processed content: {content!r}")
            logger.debug(f"Processed username: {username!r}")

            # Save message
            timestamp = datetime.now()
            if storage.save_message(username, content, timestamp):
                response = {
                    'success': True,
                    'message': 'Message saved successfully',
                    'data': {
                        'content': content,
                        'author': username,
                        'createdAt': timestamp.astimezone().isoformat(),
                        'verified': 'true',
                        'type': 'message'
                    }
                }
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response).encode('utf-8'))
            else:
                self.handle_error(500, "Failed to save message")
            
        except Exception as e:
            logger.error(f"Error handling message post: {e}")
            self.handle_error(500, str(e))

    def handle_username_post(self):
        """Handle username change request"""
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length).decode('utf-8')
            logger.debug(f"Username change request: {body}")
            
            # Parse form data
            form_data = parse_qs(body)
            new_username = form_data.get('new_username', [''])[0]
            
            if new_username:
                # Create username change message
                content = json.dumps({
                    'old_username': 'anonymous',
                    'new_username': new_username,
                    'type': 'username_change'
                })
                if storage.save_message('system', content, datetime.now()):
                    response = {
                        'success': True,
                        'message': 'Username changed successfully'
                    }
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps(response).encode('utf-8'))
                else:
                    self.handle_error(500, "Failed to save username change message")
            
            # Redirect back to home page
            self.send_response(HTTPStatus.FOUND)
            self.send_header('Location', '/')
            self.end_headers()
        except Exception as e:
            logger.error(f"Error in username change request", exc_info=True)
            self.handle_error(500, str(e))

    def handle_username_change(self):
        """Handle username change request"""
        try:
            # Get new username from request
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            new_username = data.get('username', '')
            
            # Verify new username
            if not storage.verify_username(new_username):
                self.handle_error(400, "Invalid username format")
                return
            
            # Send success response
            response = {
                'success': True,
                'username': new_username
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
            
        except Exception as e:
            logger.error(f"Error handling username change: {e}")
            self.handle_error(500, str(e))

    def serve_file(self, filepath, content_type):
        """Helper method to serve a file with specified content type"""
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            logger.error(f"File not found: {filepath}")
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as e:
            logger.error(f"Error serving file {filepath}: {e}")
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_messages(self):
        """Helper method to serve messages as JSON"""
        try:
            messages = storage.get_messages()
            
            response = {
                'messages': messages,
                'messageVerificationEnabled': MESSAGE_VERIFICATION_ENABLED
            }
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
            
        except Exception as e:
            logger.error(f"Error serving messages: {e}")
            self.handle_error(500, str(e))

    def verify_username(self):
        """Helper method to verify username"""
        try:
            query = parse_qs(urlparse(self.path).query)
            username = query.get('username', [''])[0]

            # Verify username format
            if username:
                is_valid = storage.verify_username(username)
                response = {
                    'valid': is_valid,
                    'username': username,
                    'error': None if is_valid else 'Invalid username format'
                }
            else:
                response = {
                    'valid': True,
                    'username': 'anonymous',
                    'status': 'verified'
                }

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode('utf-8'))
            
        except Exception as e:
            logger.error(f"Error verifying username: {e}")
            self.handle_error(500, str(e))

    def get_system_status(self):
        """Get current system status information."""
        try:
            # Check git status
            subprocess.run(['git', 'status'], check=True, capture_output=True)
            git_status = True
        except subprocess.CalledProcessError:
            git_status = False

        signature_status = False
        if MESSAGE_VERIFICATION_ENABLED:
            try:
                # Check if we can create and verify a test signature
                test_message = b"test"
                signature = storage.key_manager.sign_message(test_message.decode())
                signature_status = True
            except Exception:
                signature_status = False

        try:
            # Get latest commit
            result = subprocess.run(['git', 'rev-parse', 'HEAD'], 
                                 check=True, capture_output=True, text=True)
            latest_commit = result.stdout.strip()
        except subprocess.CalledProcessError:
            latest_commit = "Unknown"

        # Get list of public keys
        public_keys_dir = Path('public_keys')
        public_keys = [f.name for f in public_keys_dir.glob('*.pub')] if public_keys_dir.exists() else []

        # Get message counts
        messages = storage.get_messages()
        current_message_count = len(messages)

        return {
            'git_status': git_status,
            'signature_status': signature_status,
            'message_verification_enabled': MESSAGE_VERIFICATION_ENABLED,
            'latest_commit': latest_commit,
            'public_keys': public_keys,
            'current_time': '2025-01-13T11:24:24-05:00',  # Using provided timestamp
            'repo_name': os.environ.get('GITHUB_REPO', ''),
            'current_message_count': current_message_count,
            'archived_message_count': 0
        }

    def serve_status_page(self):
        """Serve the system status page."""
        try:
            template = self.jinja_env.get_template('status.html')
            status_data = self.get_system_status()
            content = template.render(**status_data)
            
            self.send_response(HTTPStatus.OK)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(content.encode())
        except Exception as e:
            logger.error(f"Error serving status page: {str(e)}")
            self.handle_error(500, str(e))

def find_available_port(start_port=8000, max_attempts=100):
    """Find an available port starting from start_port"""
    for port in range(start_port, start_port + max_attempts):
        try:
            with socketserver.TCPServer(("", port), None) as s:
                s.server_close()
                return port
        except OSError:
            continue
    raise RuntimeError(f"Could not find an available port after {max_attempts} attempts")

def open_browser(port):
    """Open the browser to the application URL"""
    try:
        import platform
        url = f'http://localhost:{port}'
        
        if platform.system() == 'Linux':
            # Check if running in WSL
            with open('/proc/version', 'r') as f:
                if 'microsoft' in f.read().lower():
                    # In WSL, use powershell.exe to start browser
                    os.system(f'powershell.exe -c "Start-Process \'{url}\'"')
                else:
                    # Pure Linux
                    os.system(f'xdg-open {url}')
        elif platform.system() == 'Windows':
            os.system(f'start {url}')
        elif platform.system() == 'Darwin':
            os.system(f'open {url}')
    except Exception as e:
        logger.error(f"Failed to open browser: {e}")

def main():
    """Start the server."""
    try:
        # Find available port
        port = find_available_port()
        if not port:
            logger.error("Could not find an available port")
            return

        # Create and configure the HTTP server
        handler = ChatRequestHandler
        httpd = socketserver.ThreadingTCPServer(("", port), handler)
        
        logger.info(f"Starting server on port {port}")
        print(f"Server started at http://localhost:{port}")
        
        # Open browser in a separate thread
        if not os.environ.get('NO_BROWSER'):
            threading.Thread(target=open_browser, args=(port,), daemon=True).start()
        
        # Start server
        httpd.serve_forever()
        
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
        httpd.server_close()
    except Exception as e:
        logger.error(f"Server error: {e}", exc_info=True)

if __name__ == "__main__":
    main()
