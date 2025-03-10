#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import argparse
import concurrent.futures
import json
import logging
import os
import platform
import re
import sys
import threading
import time
from configparser import ConfigParser
from datetime import date, datetime, timedelta
from importlib.resources import read_text
from pathlib import Path
from queue import Queue
from typing import Dict, List

import pyperclip
import requests
import sseclient
import tiktoken
from packaging.version import parse as parse_version
from prompt_toolkit import PromptSession, prompt
from prompt_toolkit.completion import (Completer, Completion, NestedCompleter,
                                       PathCompleter)
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.shortcuts import confirm
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import ValidationError, Validator
from rich import print as rprint
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel

from . import __version__
from .locale import set_lang, get_lang
import locale

data_dir = Path.home() / '.gpt-term'
data_dir.mkdir(parents=True, exist_ok=True)
config_path = data_dir / 'config.ini'
if not config_path.exists():
    with config_path.open('w') as f:
        f.write(read_text('gpt_term', 'config.ini'))

# Log to chat.log, comment out this line to disable logging
logging.basicConfig(filename=f'{data_dir}/chat.log', format='%(asctime)s %(name)s: %(levelname)-6s %(message)s',
                    datefmt='[%Y-%m-%d %H:%M:%S]', level=logging.INFO)

log = logging.getLogger("chat")

console = Console()

style = Style.from_dict({
    # Set prompt color to green
    "prompt": "ansigreen",
})

remote_version = None
local_version = parse_version(__version__)
threadlock_remote_version = threading.Lock()

class ChatMode:
    raw_mode = False
    multi_line_mode = False
    stream_mode = True

    @classmethod
    def toggle_raw_mode(cls):
        cls.raw_mode = not cls.raw_mode
        if cls.raw_mode:
            console.print(_("gpt_term.raw_mode_enabled"))
        else:
            console.print(_("gpt_term.raw_mode_disabled"))

    @classmethod
    def toggle_stream_mode(cls):
        cls.stream_mode = not cls.stream_mode
        if cls.stream_mode:
            console.print(
                _("gpt_term.stream_mode_enabled"))
        else:
            console.print(
                _("gpt_term.stream_mode_disabled"))

    @classmethod
    def toggle_multi_line_mode(cls):
        cls.multi_line_mode = not cls.multi_line_mode
        if cls.multi_line_mode:
            console.print(
                _("gpt_term.multi_line_enabled"))
        else:
            console.print(_("gpt_term.multi_line_disabled"))


class ChatGPT:
    def __init__(self, api_key: str, timeout: float):
        self.api_key = api_key
        self.host = "https://api.openai.com"
        self.endpoint = self.host + "/v1/chat/completions"
        self.models_endpoint = self.host + "/v1/models"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        self.thinking_mode = None  # None when disabled, int value for token budget when enabled
        self.messages = [
            {"role": "system", "content": f"You are a helpful assistant.\nCurrent date: {datetime.now().strftime('%Y-%m-%d')}"}]
        self.model = 'gpt-3.5-turbo'
        # add sensible default for bedrock
        # https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters.html#model-parameters-titan
        self.max_tokens_sampled = 5000
        self.tokens_limit = 5000
        # as default: gpt-3.5-turbo has a tokens limit as 4096
        # when model changes, tokens will also be changed
        self.temperature = 1
        self.total_tokens_spent = 0
        self.current_tokens = count_token(self.messages)
        self.timeout = timeout
        self.title: str = None
        self.gen_title_messages = Queue()
        self.auto_gen_title_background_enable = True
        self.threadlock_total_tokens_spent = threading.Lock()
        self.stream_overflow = 'ellipsis'

        self.credit_total_granted = 0
        self.credit_total_used = 0
        self.credit_used_this_month = 0
        self.credit_plan = ""

    def add_total_tokens(self, tokens: int):
        self.threadlock_total_tokens_spent.acquire()
        self.total_tokens_spent += tokens
        self.threadlock_total_tokens_spent.release()

    def send_request(self, data):
        try:
            with console.status(_("gpt_term.ChatGPT_thinking")):
                response = requests.post(
                    self.endpoint, headers=self.headers, data=json.dumps(data), timeout=self.timeout, stream=ChatMode.stream_mode)
            # Match 4xx errors to show specific error message from server
            if response.status_code // 100 == 4:
                error_msg = response.json()['error']['message']
                console.print(_("gpt_term.Error_message",error_msg=error_msg))
                log.error(error_msg)
                return None

            response.raise_for_status()
            return response
        except KeyboardInterrupt:
            console.print(_("gpt_term.Aborted"))
            raise
        except requests.exceptions.ReadTimeout as e:
            console.print(
                _("gpt_term.Error_timeout",timeout=self.timeout), highlight=False)
            return None
        except requests.exceptions.RequestException as e:
            console.print(_("gpt_term.Error_message",error_msg=str(e)))
            log.exception(e)
            return None

    def send_request_silent(self, data):
        # this is a silent sub function, for sending request without outputs (silently)
        try:
            response = requests.post(
                self.endpoint, headers=self.headers, data=json.dumps(data), timeout=self.timeout)
            # match 4xx error codes
            if response.status_code // 100 == 4:
                error_msg = response.json()['error']['message']
                log.error(error_msg)
                return None

            response.raise_for_status()
            return response
        except requests.exceptions.ReadTimeout as e:
            log.error("Automatic generating title failed as timeout")
            return None
        except requests.exceptions.RequestException as e:
            log.exception(e)
            return None

    def process_stream_response(self, response: requests.Response):
        reply: str = ""
        client = sseclient.SSEClient(response)
        final_chunk = None  # Store the final chunk
        citations = None
        thinking_content = ""
        is_thinking_mode = False  # Track if we're currently displaying thinking content
        is_thinking_complete = False
        
        with Live(console=console, auto_refresh=False, vertical_overflow=self.stream_overflow) as live:
            try:
                rprint("[bold cyan]AI: ")
                for event in client.events():
                    if event.data == '[DONE]':
                        # finish_reason = part["choices"][0]['finish_reason']
                        break
                    
                    part = json.loads(event.data)
                    log.debug(f"Stream chunk: {json.dumps(part)}")
                    
                    if 'citations' in part:
                        citations = part['citations']
                    
                    # Handle thinking content in streaming mode - more robust checking
                    if "choices" in part and len(part["choices"]) > 0 and "delta" in part["choices"][0]:
                        delta = part["choices"][0]["delta"]
                        
                        # Try different paths where reasoning content might be found
                        reasoning_content = None
                        
                        # Direct path
                        if "reasoning_content" in delta and delta["reasoning_content"]:
                            reasoning_content = delta["reasoning_content"]
                        # Check in thinking_blocks
                        elif "thinking_blocks" in delta and delta["thinking_blocks"]:
                            for block in delta["thinking_blocks"]:
                                if block.get("type") == "thinking" and "thinking" in block:
                                    reasoning_content = block["thinking"]
                                    break
                        # Check in provider_specific_fields
                        elif "provider_specific_fields" in delta and "reasoningContent" in delta["provider_specific_fields"]:
                            if "text" in delta["provider_specific_fields"]["reasoningContent"]:
                                reasoning_content = delta["provider_specific_fields"]["reasoningContent"]["text"]
                        
                        # If we found reasoning content through any path
                        if reasoning_content and not is_thinking_complete:
                            # If this is the first reasoning content chunk, print opening thinking tag
                            if not is_thinking_mode:
                                is_thinking_mode = True
                                thinking_content = "> Thought Process:\n```thinking\n"
                                
                            thinking_content += reasoning_content
                            
                            if ChatMode.raw_mode:
                                # Use brighter yellow for better visibility
                                rprint(reasoning_content, end="", style="yellow", flush=True)
                            else:
                                # Ensure the thinking content is displayed with proper styling
                                live.update(Markdown(f"{thinking_content}"), refresh=True)
                        
                        # Process regular content
                        if "content" in delta and delta['content']:
                            log.debug(f"Delta Content: {delta['content']}")
                            log.debug(f"Thinking Content: {thinking_content}")
                            
                            content = delta["content"]
                            
                            # Replace <think> and </think> markers for specific models
                            if "sonar-reasoning-pro" in self.model or "deepseek-r1" in self.model:
                                content = content.replace("<think>", "> Thought Process:\n```thinking\n")
                                content = content.replace("</think>", "\n```\n\n> AI Response:  \n\n")
                            
                            # If we were displaying thinking content and now we have regular content,
                            # close the thinking tag first
                            if is_thinking_mode and not is_thinking_complete:
                                is_thinking_mode = False
                                thinking_content += "\n```\n\n"
                                is_thinking_complete = True
                                reply += f"{thinking_content}\n> AI Response:  \n\n"
                                log.debug("Thinking Completed")
                            reply += content
                            if ChatMode.raw_mode:
                                rprint(content, end="", flush=True)
                            else:
                                #TODO: change citations to print only at the end
                                if citations:
                                    reply_full = reply + format_citations(citations)
                                else:
                                    reply_full = reply
                                log.debug(f"Reply Full: {reply_full}")
                                live.update(Markdown(reply_full), refresh=True)
                    
                    final_chunk = part  # Keep track of the final chunk
            except KeyboardInterrupt:
                live.stop()
                console.print(_('gpt_term.Aborted'))
            except Exception as e:
                live.stop()
                log.debug(f"Exception: {e}")
                rprint(f"[red]Error processing response: {str(e)}[/red]")
            finally:
                reply_message = {'role': 'assistant', 'content': reply}
                
                return reply_message

    def process_response(self, response: requests.Response):
        if ChatMode.stream_mode:
            return self.process_stream_response(response)
        else:
            response_json = response.json()
            log.debug(f"Response: {response_json}")
            reply_message: Dict[str, str] = response_json["choices"][0]["message"]
            
            # Check for citations in the response
            if "citations" in response_json:
                reply_message["citations"] = response_json["citations"]
            
            # Check for thinking content in non-stream mode
            if "thinking" in response_json:
                reply_message["thinking"] = response_json["thinking"]
                console.print("[dim italic]Thinking content captured in response.[/dim italic]")
            
            print_message(reply_message)
            return reply_message

    def delete_first_conversation(self):
        if len(self.messages) >= 3:
            question = self.messages[1]
            del self.messages[1]
            if self.messages[1]['role'] == "assistant":
                # Delete if the second message is an answer
                del self.messages[1]
            truncated_question = question['content'].split('\n')[0]
            if len(question['content']) > len(truncated_question):
                truncated_question += "..."

            # recount current tokens
            new_tokens = count_token(self.messages)
            tokens_saved = self.current_tokens - new_tokens
            self.current_tokens = new_tokens

            console.print(
                _('gpt_term.delete_first_conversation_yes',truncated_question=truncated_question,tokens_saved=tokens_saved))
        else:
            console.print(_('gpt_term.delete_first_conversation_no'))
    
    def delete_all_conversation(self):
        del self.messages[1:]
        self.title = None
        # recount current tokens
        self.current_tokens = count_token(self.messages)
        os.system('cls' if os.name == 'nt' else 'clear')
        console.print(_('gpt_term.delete_all'))

    def handle_simple(self, message: str):
        self.messages.append({"role": "user", "content": message})
        data = {
            "model": self.model,
            "messages": self.messages,
            "temperature": self.temperature
        }
        response = self.send_request_silent(data)
        if response:
            response_json = response.json()
            log.debug(f"Response: {response_json}")
            print(response_json["choices"][0]["message"]["content"])

    def handle(self, message: str):
        try:
            self.messages.append({"role": "user", "content": message})
            data = {
                "model": self.model,
                "messages": self.messages,
                "stream": ChatMode.stream_mode,
                "temperature": self.temperature
            }
            
            # Add thinking mode parameters only for supported Claude 3.7 Sonnet models
            if self.thinking_mode is not None and "bedrock/anthropic.claude-3-7-sonnet" in self.model:
                data["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.thinking_mode
                }
                
            if 'bedrock/anthropic' or 'bedrock/amazon' or "anthropic/" in self.model:
                data['max_tokens'] = self.max_tokens_sampled
            response = self.send_request(data)
            if response is None:
                self.messages.pop()
                if self.current_tokens >= self.tokens_limit:
                    console.print(_('gpt_term.tokens_reached'))
                return

            reply_message = self.process_response(response)
            if reply_message is not None:
                log.info(f"ChatGPT: {reply_message['content']}")
                self.messages.append(reply_message)
                self.current_tokens = count_token(self.messages)
                self.add_total_tokens(self.current_tokens)

                if len(self.messages) == 3 and self.auto_gen_title_background_enable:
                    self.gen_title_messages.put(self.messages[1]['content'])

                if self.tokens_limit - self.current_tokens in range(1, 500):
                    console.print(
                        _("gpt_term.tokens_approaching",token_left=self.tokens_limit - self.current_tokens))
                # approaching tokens limit (less than 500 left), show info

        except Exception as e:
            console.print(
                _("chat_term.Error_look_log",error_msg=str(e)))
            log.exception(e)
            self.save_chat_history_urgent()
            raise EOFError

        return reply_message

    def gen_title(self, force: bool = False):
        # Empty the title if there is only system message left
        if len(self.messages) < 2:
            self.title = None
            return

        try:
            with console.status(_("gpt_term.title_waiting_gen")):
                self.gen_title_messages.join()
            if self.title and not force:
                return self.title

            # title not generated, do

            content_this_time = self.messages[1]['content']
            self.gen_title_messages.put(content_this_time)
            with console.status(_("gpt_term.title_gening")):
                self.gen_title_messages.join()
        except KeyboardInterrupt:
            console.print(_("gpt_term.title_skip_gen"))
            raise

        return self.title

    def gen_title_silent(self, content: str):
        # this is a silent sub function, only for sub thread which auto-generates title when first conversation is made and debug functions
        # it SHOULD NOT be triggered or used by any other functions or commands
        # because of the usage of this subfunction, no check for messages list length and title appearance is needed
        prompt = f'Generate title shorter than 10 words for the following content in content\'s language. The tilte contains ONLY words. DO NOT include line-break. \n\nContent: """\n{content}\n"""'
        messages = [{"role": "user", "content": prompt}]
        data = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.5
        }
        response = self.send_request_silent(data)
        if response is None:
            self.title = None
            return
        reply_message = response.json()["choices"][0]["message"]
        self.title: str = reply_message['content']
        # here: we don't need a lock here for self.title because: the only three places changes or uses chat_gpt.title will never operate together
        # they are: gen_title, gen_title_silent (here), '/save' command
        log.debug(f"Title background silent generated: {self.title}")

        messages.append(reply_message)
        self.add_total_tokens(count_token(messages))
        # count title generation tokens cost

        return self.title

    def auto_gen_title_background(self):
        # this is the auto title generation daemon thread main function
        # it SHOULD NOT be triggered or used by any other functions or commands
        while True:
            try:
                content_this_time = self.gen_title_messages.get()
                log.debug(f"Title Generation Daemon Thread: Working with message \"{content_this_time}\"")
                new_title = self.gen_title_silent(content_this_time)
                self.gen_title_messages.task_done()
                time.sleep(0.2)
                if not new_title:
                    log.error("Background Title auto-generation Failed")
                else:
                    change_CLI_title(self.title)
                log.debug("Title Generation Daemon Thread: Pause")

            except Exception as e:
                console.print(_("gpt_term.title_auto_gen_fail",error_msg=str(e))
                    )
                log.exception(e)
                self.save_chat_history_urgent()
                while self.gen_title_messages.unfinished_tasks:
                    self.gen_title_messages.task_done()
                continue
                # something went wrong, continue the loop

    def save_chat_history(self, filename):
        try:
            with open(f"{filename}", 'w', encoding='utf-8') as f:
                json.dump(self.messages, f, ensure_ascii=False, indent=4)
            console.print(
                _("gpt_term.save_history_success",filename=filename), highlight=False)
        except Exception as e:
            console.print(
                _("gpt_term.Error_look_log"))
            log.exception(e)
            self.save_chat_history_urgent()
            return

    def save_chat_history_urgent(self):
        filename = f'{data_dir}/chat_history_backup_{datetime.now().strftime("%Y-%m-%d_%H,%M,%S")}.json'
        with open(f"{filename}", 'w', encoding='utf-8') as f:
            json.dump(self.messages, f, ensure_ascii=False, indent=4)
        console.print(
            _("gpt_term.save_history_urgent_success",filename=filename), highlight=False)

    def send_get(self, url, params=None):
        try:
            response = requests.get(
                url, headers=self.headers, timeout=self.timeout, params=params)

            # Handle 4xx errors by displaying the specific reason returned by the server
            if response.status_code // 100 == 4:
                error_msg = response.json()['error']['message']
                console.print(_("gpt_term.Error_get_url",url=url,error_msg=error_msg))
                log.error(error_msg)
                return None
            response.raise_for_status()
            return response
        except KeyboardInterrupt:
            console.print(_("gpt_term.Aborted"))
            raise
        except requests.exceptions.ReadTimeout as e:
            console.print(
                _("gpt_term.Error_timeot",timeout=self.timeout), highlight=False)
            return None
        except requests.exceptions.RequestException as e:
            console.print(_("gpt_term.Error_message",error_msg=str(e)))
            log.exception(e)
            return None

    def fetch_credit_total_granted(self):
        url_subscription = self.host + "/dashboard/billing/subscription"
        response_subscription = self.send_get(url_subscription)
        if not response_subscription:
            self.credit_total_granted = None
        response_subscription_json = response_subscription.json()
        self.credit_total_granted = response_subscription_json["hard_limit_usd"]
        self.credit_plan = response_subscription_json["plan"]["title"]

    def fetch_credit_monthly_used(self, url_usage):
        usage_get_params_monthly = {
            "start_date": str(date.today().replace(day=1)),
            "end_date": str(date.today() + timedelta(days=1))}
        response_monthly_usage = self.send_get(
            url_usage, params=usage_get_params_monthly)
        if not response_monthly_usage:
            self.credit_used_this_month = None
        self.credit_used_this_month = response_monthly_usage.json()[
            "total_usage"] / 100

    def get_credit_usage(self):
        url_usage = "https://platform.openai.com/usage"
        console.print(f"Please go to {url_usage} to check usage.")
        return False
    
    def set_host(self, host: str):
        self.host = host
        #if api_key includes litellm, set endpoint to remove /v1/ from endpoint
        if "litellm" in self.api_key:
            self.endpoint = self.host + "/chat/completions"
            self.models_endpoint = self.host + "/models"
        else:
            self.endpoint = self.host + "/v1/chat/completions"
            self.models_endpoint = self.host + "/v1/models"

    def modify_system_prompt(self, new_content: str):
        if self.messages[0]['role'] == 'system':
            old_content = self.messages[0]['content']
            self.messages[0]['content'] = new_content
            console.print(
                _("gpt_term.system_prompt_modified",old_content=old_content,new_content=new_content))
            self.current_tokens = count_token(self.messages)
            # recount current tokens
            if len(self.messages) > 1:
                console.print(
                    _("gpt_term.system_prompt_note"))
        else:
            console.print(
                _("gpt_term.system_prompt_found"))

    def set_stream_overflow(self, new_overflow: str):
        # turn on stream if not
        if not ChatMode.stream_mode:
            ChatMode.toggle_stream_mode()

        if new_overflow == self.stream_overflow:
            console.print(_("gpt_term.No_change"))
            return

        old_overflow = self.stream_overflow
        if new_overflow == 'ellipsis' or new_overflow == 'visible':
            self.stream_overflow = new_overflow
            console.print(
                _("gpt_term.stream_overflow_modified",old_overflow=old_overflow,new_overflow=new_overflow))
            if new_overflow == 'visible':
                console.print(_("gpt_term.stream_overflow_visible"))
        else:
            console.print(_("gpt_term.stream_overflow_no_changed",old_overflow=old_overflow))

    @property
    def available_models(self) -> set:
        """Fetch available models from the API"""
        try:
            response = self.send_get(self.models_endpoint)
            if response:
                models = response.json()["data"]
                # Get all model IDs since capabilities field is not reliable
                model_ids = {m["id"] for m in models}
                return model_ids
            return set()
        except Exception as e:
            log.error(f"Failed to fetch models: {str(e)}")
            return set()

    def set_model(self, new_model: str):
        old_model = self.model
        if not new_model:
            console.print(_("gpt_term.model_set"), old_model=old_model)
            return
        
        # Allow any model if API didn't return models or if it's a known model type
        if (self.available_models and 
            new_model not in self.available_models and
            not any(prefix in new_model for prefix in ['bedrock/', 'anthropic/', 'claude-'])):
            console.print(_("gpt_term.model_not_available"))
            return
            
        self.model = str(new_model)
        if "gpt-4-1106-preview" in self.model:
            self.tokens_limit = 128000
        elif "gpt-4-vision-preview" in self.model:
            self.tokens_limit = 128000
        elif "gpt-4o" in self.model:
            self.tokens_limit = 128000
        elif "gpt-4-32k" in self.model:
            self.tokens_limit = 32768
        elif "gpt-4" in self.model:
            self.tokens_limit = 8192
        elif "gpt-3.5-turbo-16k" in self.model:
            self.tokens_limit = 16385
        elif "gpt-3.5-turbo-1106" in self.model:
            self.tokens_limit = 16385
        elif "gpt-3.5-turbo" in self.model:
            self.tokens_limit = 4096
        elif "bedrock/anthropic" or "anthropic/" in self.model:
            self.tokens_limit = 200000
        elif "bedrock/cohere" in self.model:
            self.tokens_limit = 4096
        elif "bedrock/ai21" in self.model:
            self.tokens_limit = 8192
        elif "bedrock/amazon.nova" in self.model:
            self.tokens_limit = 200000
        else:
            self.tokens_limit = float('nan')
        console.print(
            _("gpt_term.model_changed",old_model=old_model,new_model=new_model))

    def set_timeout(self, timeout):
        try:
            self.timeout = float(timeout)
        except ValueError:
            console.print(_("gpt_term.Error_input_number"))
            return
        console.print(_("gpt_term.timeput_changed",timeout=timeout))

    def set_temperature(self, temperature):
        try:
            new_temperature = float(temperature)
        except ValueError:
            console.print(_("gpt_term.temperature_must_between"))
            return
        if new_temperature > 1 or new_temperature < 0:
            console.print(_("gpt_term.temperature_must_between"))
            return
        self.temperature = new_temperature
        console.print(_("gpt_term.temperature_set",temperature=temperature))

class CommandCompleter(Completer):
    def __init__(self, chat_gpt):
        self.chat_gpt = chat_gpt

    @property
    def nested_completer(self):
        # Initialize with basic command structure and available models
        command_dict = {
            '/raw': None,
            '/multi': None,
            '/stream': {"visible", "ellipsis"},
            '/tokens': None,
            '/usage': None,
            '/last': None,
            '/copy': {"code", "all"},
            '/model': self.chat_gpt.available_models,
            '/save': PathCompleter(file_filter=self.path_filter),
            '/system': None,
            '/rand': None,
            '/temperature': None,
            '/thinking': {"off", "disable", "2048", "4096", "8192"},
            '/title': None,
            '/timeout': None,
            '/undo': None,
            '/delete': {"first", "all"},
            '/reset': None,
            '/lang': {"zh_CN", "en", "jp", "de"},
            '/version': None,
            '/help': None,
            '/exit': None,
        }
        return NestedCompleter.from_nested_dict(command_dict)

    def path_filter(self, filename):
        # Auto-complete paths, only complete json files and directories
        return filename.endswith(".json") or os.path.isdir(filename)

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith('/'):
            for cmd in self.nested_completer.options.keys():
                # If first level command matches
                if text in cmd:
                    yield Completion(cmd, start_position=-len(text))
            # If nth level command matches
            if ' ' in text:
                for sub_cmd in self.nested_completer.get_completions(document, complete_event):
                    yield sub_cmd



def count_token(messages: List[Dict[str, str]]):
    encoding = tiktoken.get_encoding("cl100k_base")
    length = 0
    for message in messages:
        length += len(encoding.encode(str(message)))
    return length


class NumberValidator(Validator):
    def validate(self, document):
        text = document.text
        if not text.isdigit():
            raise ValidationError(message=_("gpt_term.Error_input_int"),
                                  cursor_position=len(text))

class FloatRangeValidator(Validator):
    def __init__(self, min_value=None, max_value=None):
        self.min_value = min_value
        self.max_value = max_value

    def validate(self, document):
        try:
            value = float(document.text)
        except ValueError:
            raise ValidationError(message=_('gpt_term.Error_input_number'))

        if self.min_value is not None and value < self.min_value:
            raise ValidationError(message=_("gpt_term.Error_input_least",min_value=self.min_value))
        if self.max_value is not None and value > self.max_value:
            raise ValidationError(message=_("gpt_term.Error_input_most",max_value=self.max_value))
        
temperature_validator = FloatRangeValidator(min_value=0.0, max_value=2.0)

def print_citations(citations: List[str]):
    if not citations:
        return
    console.print("\nCitations:")
    for i, citation in enumerate(citations, 1):
        console.print(f"[{i}] {citation}")

def format_citations(citations: List[str]):
    if not citations:
        return ""
    citation_text = "\n\n> Citations:  \n\n"
    for i, citation in enumerate(citations, 1):
        citation_text += f"[{i}] {citation}  \n"
    return citation_text

def print_message(message: Dict[str, str]):
    role = message["role"]
    content = message["content"]
    if role == "user":
        print(f"> {content}")
    #TODO: add more specificity on which model to the response
    elif role == "assistant":
        console.print("AI: ", end='', style="bold cyan")
        if ChatMode.raw_mode:
            print(content)
        else:
            console.print(Markdown(content), new_line_start=True)
        if "citations" in message:
            print_citations(message["citations"])
        if "thinking" in message:
            console.print("[dim italic]Thinking:[/dim italic]")
            console.print(Panel(Markdown(message["thinking"]), 
                                title="[dim]Thinking Process[/dim]", 
                                border_style="dim", 
                                width=100))


def copy_code(message: Dict[str, str], select_code_idx: int = None):
    '''Copy the code in ChatGPT's last reply to Clipboard'''
    code_list = re.findall(r'```[\s\S]*?```', message["content"])
    if len(code_list) == 0:
        console.print(_("gpt_term.code_not_found"))
        return

    if len(code_list) == 1 and select_code_idx is None:
        selected_code = code_list[0]
        # if there's only one code, and select_code_idx not given, just copy it
    else:
        if select_code_idx is None:
            console.print(
                _("gpt_term.code_too_many_found"))
            code_num = 0
            for codes in code_list:
                code_num += 1
                console.print(_("gpt_term.code_num",code_num=code_num))
                console.print(Markdown(codes))

            select_code_idx = prompt(
                _("gpt_term.code_select"), style=style, validator=NumberValidator())
            # get the number of the selected code
        try:
            selected_code = code_list[int(select_code_idx)-1]
        except ValueError:
            console.print(_("gpt_term.code_index_must_int"))
            return
        except IndexError:
            if len(code_list) == 1:
                console.print(
                    _("gpt_term.code_index_out_range_one"))
            else:
                console.print(
                    _("gpt_term.code_index_out_range_many",len(code_list)))
                # show idx range
                # use len(code_list) instead of code_num as the max of idx
                # in order to avoid error 'UnboundLocalError: local variable 'code_num' referenced before assignment' when inputing select_code_idx directly
            return

    bpos = selected_code.find('\n')    # code begin pos.
    epos = selected_code.rfind('```')  # code end pos.
    pyperclip.copy(''.join(selected_code[bpos+1:epos-1]))
    # erase code begin and end sign
    console.print(_("gpt_term.code_copy"))


def change_CLI_title(new_title: str):
    if platform.system() == "Windows":
        os.system(f"title {new_title}")
    else:
        print(f"\033]0;{new_title}\007", end='')
        sys.stdout.flush()
        # flush the stdout buffer in order to making the control sequences effective immediately
    log.debug(f"CLI Title changed to '{new_title}'")

def get_levenshtein_distance(s1: str, s2: str):
    s1_len = len(s1)
    s2_len = len(s2)

    v = [[0 for _ in range(s2_len+1)] for _ in range(s1_len+1)]
    for i in range(0, s1_len+1):
        for j in range(0, s2_len+1):
            if i == 0:
                v[i][j] = j
            elif j == 0:
                v[i][j] = i
            elif s1[i-1] == s2[j-1]:
                v[i][j] = v[i-1][j-1]
            else:
                v[i][j] = min(v[i-1][j-1], min(v[i][j-1], v[i-1][j])) + 1

    return v[s1_len][s2_len]


def handle_command(command: str, chat_gpt: ChatGPT, key_bindings: KeyBindings, chat_save_perfix: str, command_completer: CommandCompleter):
    '''Handle slash (/) commands'''
    global _
    if command == '/raw':
        ChatMode.toggle_raw_mode()
    elif command == '/multi':
        ChatMode.toggle_multi_line_mode()
    elif command.startswith('/thinking'):
        args = command.split()
        
        # Check if the model supports thinking mode
        is_supported_model = ("bedrock/anthropic.claude-3-7-sonnet" in chat_gpt.model)
        
        if not is_supported_model:
            console.print("Thinking mode is only supported with Bedrock Claude 3.7 Sonnet models.", style="yellow")
            console.print("Supported models include: bedrock/anthropic.claude-3-7-sonnet-*", style="yellow")
            return
            
        # Parse arguments
        if len(args) > 1:
            try:
                budget = int(args[1])
                chat_gpt.thinking_mode = budget
                console.print(f"Thinking mode enabled with budget of {budget} tokens.", style="green")
            except ValueError:
                if args[1].lower() in ["off", "disable", "disabled", "false", "no"]:
                    chat_gpt.thinking_mode = None
                    console.print("Thinking mode disabled.", style="yellow")
                else:
                    # Default to 2048 if not a valid number
                    chat_gpt.thinking_mode = 2048
                    console.print("Thinking mode enabled with default budget of 2048 tokens.", style="green")
        else:
            # Toggle thinking mode
            if chat_gpt.thinking_mode is None:
                chat_gpt.thinking_mode = 2048  # Default budget
                console.print("Thinking mode enabled with default budget of 2048 tokens.", style="green")
            else:
                chat_gpt.thinking_mode = None
                console.print("Thinking mode disabled.", style="yellow")

    elif command.startswith('/stream'):
        args = command.split()
        if len(args) > 1:
            chat_gpt.set_stream_overflow(args[1])
        else:
            ChatMode.toggle_stream_mode()

    elif command == '/tokens':
        chat_gpt.threadlock_total_tokens_spent.acquire()
        console.print(Panel(_("gpt_term.tokens_used",total_tokens_spent=chat_gpt.total_tokens_spent,current_tokens=chat_gpt.current_tokens,tokens_limit=chat_gpt.tokens_limit),
                            title=_("gpt_term.tokens_title"), title_align='left', width=40))
        chat_gpt.threadlock_total_tokens_spent.release()

    elif command == '/usage':
        with console.status(_("gpt_term.usage_getting")):
            if not chat_gpt.get_credit_usage():
                return
        console.print(Panel(f'{_("gpt_term.usage_granted",credit_total_granted=format(chat_gpt.credit_total_granted, ".2f"))}\n'
                            f'{_("gpt_term.usage_used_month",credit_used_this_month=format(chat_gpt.credit_used_this_month, ".2f"))}\n'
                            f'{_("gpt_term.usage_total",credit_total_used=format(chat_gpt.credit_total_used, ".2f"))}',
                            title=_("gpt_term.usage_title"), title_align='left', subtitle=_("gpt_term.usage_plan",credit_plan=chat_gpt.credit_plan), width=35))

    elif command.startswith('/model'):
        args = command.split()
        if len(args) > 1:
            new_model = args[1]
        else:
            new_model = prompt(
                "OpenAI API model: ", default=chat_gpt.model, style=style)
        if new_model != chat_gpt.model:
            chat_gpt.set_model(new_model)
        else:
            console.print(_("gpt_term.No_change"))

    elif command == '/last':
        reply = chat_gpt.messages[-1]
        print_message(reply)

    elif command.startswith('/copy'):
        args = command.split()
        reply = chat_gpt.messages[-1]
        if len(args) > 1:
            if args[1] == 'all':
                pyperclip.copy(reply["content"])
                console.print(_("gpt_term.code_last_copy"))
            elif args[1] == 'code':
                if len(args) > 2:
                    copy_code(reply, args[2])
                else:
                    copy_code(reply)
            else:
                console.print(
                    _("gpt_term.code_copy_fail"))
        else:
            pyperclip.copy(reply["content"])
            console.print(_("gpt_term.code_last_copy"))

    elif command.startswith('/save'):
        args = command.split()
        if len(args) > 1:
            filename = args[1]
        else:
            gen_filename = chat_gpt.gen_title()
            if gen_filename:
                gen_filename = re.sub(r'[\/\\\*\?\"\<\>\|\:]', '', gen_filename)
                gen_filename = f"{chat_save_perfix}{gen_filename}.json"
            # here: if title is already generated or generating, just use it
            # but title auto generation can also be disabled; therefore when title is not generated then try generating a new one
            date_filename = f'{chat_save_perfix}{datetime.now().strftime("%Y-%m-%d_%H,%M,%S")}.json'
            filename = prompt(
                "Save to: ", default=gen_filename or date_filename, style=style)
        chat_gpt.save_chat_history(filename)

    elif command.startswith('/system'):
        args = command.split()
        if len(args) > 1:
            new_content = ' '.join(args[1:])
        else:
            new_content = prompt(
                _("gpt_term.system_prompt"), default=chat_gpt.messages[0]['content'], style=style, key_bindings=key_bindings)
        if new_content != chat_gpt.messages[0]['content']:
            chat_gpt.modify_system_prompt(new_content)
        else:
            console.print(_("gpt_term.No_change"))

    elif command.startswith('/rand') or command.startswith('/temperature'):
        args = command.split()
        if len(args) > 1:
            new_temperature = args[1]
        else:
            new_temperature = prompt(
                _("gpt_term.new_temperature"), default=str(chat_gpt.temperature), style=style, validator=temperature_validator)
        if new_temperature != str(chat_gpt.temperature):
            chat_gpt.set_temperature(new_temperature)
        else:
            console.print(_("gpt_term.No_change"))            

    elif command.startswith('/title'):
        args = command.split()
        if len(args) > 1:
            chat_gpt.title = ' '.join(args[1:])
            change_CLI_title(chat_gpt.title)
        else:
            # generate a new title
            new_title = chat_gpt.gen_title(force=True)
            if not new_title:
                console.print(_("gpt_term.title_gen_fail"))
                return
        console.print(_('gpt_term.title_changed',title=chat_gpt.title))

    elif command.startswith('/timeout'):
        args = command.split()
        if len(args) > 1:
            new_timeout = args[1]
        else:
            new_timeout = prompt(
                _("gpt_term.timeout_prompt"), default=str(chat_gpt.timeout), style=style)
        if new_timeout != str(chat_gpt.timeout):
            chat_gpt.set_timeout(new_timeout)
        else:
            console.print(_("gpt_term.No_change"))

    elif command == '/undo':
        if len(chat_gpt.messages) > 2:
            question = chat_gpt.messages.pop()
            if question['role'] == "assistant":
                question = chat_gpt.messages.pop()
            truncated_question = question['content'].split('\n')[0]
            if len(question['content']) > len(truncated_question):
                truncated_question += "..."
            console.print(
                _("gpt_term.undo_removed",truncated_question=truncated_question))
            chat_gpt.current_tokens = count_token(chat_gpt.messages)
        else:
            console.print(_("gpt_term.undo_nothing"))

    elif command.startswith('/reset'):
        chat_gpt.delete_all_conversation()

    elif command.startswith('/delete'):
        args = command.split()
        if len(args) > 1:
            if args[1] == 'first':
                chat_gpt.delete_first_conversation()
            elif args[1] == 'all':
                chat_gpt.delete_all_conversation()
            else:
                console.print(
                    _("gpt_term.delete_nothing"))
        else:
            chat_gpt.delete_first_conversation()

    elif command == '/version':
        threadlock_remote_version.acquire()
        string=_("gpt_term.version_all",local_version=str(local_version),remote_version=str(remote_version))
        console.print(Panel(string,
                            title=_("gpt_term.version_name"), title_align='left', width=28))
        threadlock_remote_version.release()
    
    elif command.startswith('/lang'):
        args = command.split()
        if len(args) > 1:
            new_lang = args[1]
        else:
            new_lang = prompt(
                _("gpt_term.new_lang_prompt"), default=get_lang(), style=style)
        if new_lang != get_lang():
            if new_lang in supported_langs:
                _=set_lang(new_lang)
                console.print(_("gpt_term.lang_switch"))
            else:
                console.print(_("gpt_term.lang_unsupport", new_lang=new_lang))
        else:
            console.print(_("gpt_term.No_change"))

    elif command == '/exit':
        raise EOFError

    elif command == '/help':
        help_text = _("""gpt_term.help_text""")
        help_text += "\n/thinking [budget]: Toggle thinking mode for Bedrock Claude 3.7 Sonnet models (default: 2048 tokens)"
        console.print(help_text)
        
    else:
        set_command = set(command)
        min_levenshtein_distance = len(command)
        most_similar_command = ""
        for slash_command in command_completer.nested_completer.options.keys():
            this_levenshtein_distance = get_levenshtein_distance(command, slash_command)
            if this_levenshtein_distance < min_levenshtein_distance:
                set_slash_command = set(slash_command)
                if len(set_command & set_slash_command) / len(set_command | set_slash_command) >= 0.75:
                    most_similar_command = slash_command
                    min_levenshtein_distance = this_levenshtein_distance
        
        console.print(_("gpt_term.help_uncommand",command=command), end=" ")
        if most_similar_command:
            console.print(_("gpt_term.help_mean_command",most_similar_command=most_similar_command))
        else:
            console.print("")
        console.print(_("gpt_term.help_use_help"))


def load_chat_history(file_path):
    '''Load chat history from file_path'''
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            chat_history = json.load(f)
        return chat_history
    except FileNotFoundError:
        console.print(_("gpt_term.load_file_not",file_path=file_path))
    except json.JSONDecodeError:
        console.print(_("gpt_term.load_json_error",file_path=file_path))
    return None


def create_key_bindings():
    '''Custom Enter key binding to ignore multi-line mode for slash commands, and use `esc+Enter` for line break in single-line mode'''
    key_bindings = KeyBindings()

    @key_bindings.add(Keys.Enter)
    def _(event):
        buffer = event.current_buffer
        text = buffer.text.strip()
        if text.startswith('/') or not ChatMode.multi_line_mode:
            buffer.validate_and_handle()
        else:
            buffer.insert_text('\n')

    @key_bindings.add(Keys.Escape, Keys.Enter)
    def _(event):
        buffer = event.current_buffer
        if ChatMode.multi_line_mode:
            buffer.validate_and_handle()
        else:
            buffer.insert_text('\n')
    return key_bindings

def get_remote_version():
    global remote_version
    try:
        response = requests.get(
            "https://pypi.org/pypi/gpt-term/json", timeout=10)
        response.raise_for_status()
        threadlock_remote_version.acquire()
        remote_version = parse_version(response.json()["info"]["version"])
        threadlock_remote_version.release()
    except requests.RequestException as e:
        log.error("Get remote version failed")
        log.exception(e)
        return
    log.debug(f"Remote version: {str(remote_version)}")


def write_config(config_ini: ConfigParser):
    with open(f'{data_dir}/config.ini', 'w') as configfile:
        config_ini.write(configfile)


def set_config_by_args(args: argparse.Namespace, config_ini: ConfigParser):
    global _
    config_need_to_set = {}
    if args.set_model:      config_need_to_set.update({"OPENAI_MODEL"         : args.set_model})
    if args.set_host:       config_need_to_set.update({"OPENAI_HOST"         : args.set_host})
    if args.set_apikey:     config_need_to_set.update({"OPENAI_API_KEY"      : args.set_apikey})
    if args.set_timeout:    config_need_to_set.update({"OPENAI_API_TIMEOUT"  : args.set_timeout})
    if args.set_saveperfix: config_need_to_set.update({"CHAT_SAVE_PERFIX"    : args.set_saveperfix})
    if args.set_loglevel:   config_need_to_set.update({"LOG_LEVEL"           : args.set_loglevel})
    if args.set_gentitle:   config_need_to_set.update({"AUTO_GENERATE_TITLE" : args.set_gentitle})
    # 新的语言设置:
    if args.set_lang:       
        config_need_to_set.update({"LANGUAGE": args.set_lang})
        _=set_lang(args.set_lang)
    # here: when set lang is called, set language before printing 'set-successful' messages

    if len(config_need_to_set) == 0:
        return
    # nothing to set
    for key, val in config_need_to_set.items():
        config_ini['DEFAULT'][key] = str(val)
        console.print(_("gpt_term.config_key_to_shell_key",key_word=str(key),val=str(val)))

    write_config(config_ini)
    exit(0)


def main():
    global _, supported_langs
    supported_langs = ["en","zh_CN","jp","de"]
    local_lang = locale.getdefaultlocale()[0]
    if local_lang not in supported_langs:
        local_lang = "en"
    _=set_lang(local_lang)

    config_ini = ConfigParser()
    config_ini.read(f'{data_dir}/config.ini', encoding='utf-8')
    config = config_ini['DEFAULT']

    config_lang = config.get("language")
    if config_lang:
        if config_lang in supported_langs:
            _=set_lang(config_lang)
        else:
            console.print(_("gpt_term.lang_config_unsupport", config_lang=config_lang))
        # if lang set in config is not support, print infos and use default local_lang

    parser = argparse.ArgumentParser(description=_("gpt_term.help_description"),add_help=False)
    parser.add_argument('-h', '--help',action='help', help=_("gpt_term.help_help"))
    parser.add_argument('-v','--version', action='version', version=f'%(prog)s v{local_version}',help=_("gpt_term.help_v"))
    parser.add_argument('--load', metavar='FILE', type=str, help=_("gpt_term.help_load"))
    parser.add_argument('--key', type=str, help=_("gpt_term.help_key"))
    parser.add_argument('--model', type=str, help=_("gpt_term.help_model"))
    parser.add_argument('--host', metavar='HOST', type=str, help=_("gpt_term.help_host"))
    parser.add_argument('-m', '--multi', action='store_true', help=_("gpt_term.help_m"))
    parser.add_argument('-r', '--raw', action='store_true', help=_("gpt_term.help_r"))
    parser.add_argument('-l','--lang', type=str, choices=['en', 'zh_CN', 'jp', 'de'], help=_("gpt_term.help_lang"))
    # normal function args

    parser.add_argument('--set-model', metavar='MODEL', type=str, help=_("gpt_term.help_set_model"))
    parser.add_argument('--set-host', metavar='HOST', type=str, help=_("gpt_term.help_set_host"))
    parser.add_argument('--set-apikey', metavar='KEY', type=str, help=_("gpt_term.help_set_key"))
    parser.add_argument('--set-timeout', metavar='SEC', type=int, help=_("gpt_term.help_set_timeout"))
    parser.add_argument('--set-gentitle', metavar='BOOL', type=str, help=_("gpt_term.help_set_gentitle"))
    parser.add_argument('--set-lang', type=str, choices=['en', 'zh_CN', 'jp', 'de'], help=_("gpt_term.help_set_lang"))
    parser.add_argument('--set-saveperfix', metavar='PERFIX', type=str, help=_("gpt_term.help_set_saveperfix"))
    parser.add_argument('--set-loglevel', metavar='LEVEL', type=str, help=_("gpt_term.help_set_loglevel")+'DEBUG, INFO, WARNING, ERROR, CRITICAL')
    # Query without parameter
    parser.add_argument("query", nargs="*", help=_("gpt_term.help_direct_query"))
    # setting args
    args = parser.parse_args()

    set_config_by_args(args, config_ini)

    if args.lang:
        _=set_lang(args.lang)
        console.print(_("gpt_term.lang_switch"))

    try:
        log_level = getattr(logging, config.get("LOG_LEVEL", "INFO").upper())
    except AttributeError as e:
        console.print(
            _("gpt_term.log_level_error"))
        log_level = logging.INFO
    log.setLevel(log_level)
    # log level set must be before debug logs, because default log level is INFO, and before new log level being set debug logs will not be written to log file

    log.info("GPT-Term start")
    log.debug(f"Local version: {str(local_version)}")
    # get local version from pkg resource

    check_remote_update_thread = threading.Thread(target=get_remote_version, daemon=True)
    check_remote_update_thread.start()
    log.debug("Remote version get thread started")
    # try to get remote version and check update

    # if 'key' arg triggered, load the api key from config.ini with the given key-name;
    # otherwise load the api key with the key-name "OPENAI_API_KEY"
    if args.key:
        #check if key starts with sk-
        if args.key.startswith("sk-"):
            api_key = args.key
        else:
            log.debug(f"Try loading API key with {args.key} from config.ini")
            api_key = config.get(args.key)
    else:
        api_key = config.get("OPENAI_API_KEY")

    if not api_key:
        log.debug("API Key not found, waiting for input")
        api_key = prompt(_("gpt_term.input_api_key"))
        if confirm(_("gpt_term.save_api_key"), suffix=" (y/N) "):
            config["OPENAI_API_KEY"] = api_key
            write_config(config_ini)

    api_key_log = api_key[:3] + '*' * (len(api_key) - 7) + api_key[-4:]
    log.debug(f"Loaded API Key: {api_key_log}")

    api_timeout = config.getfloat("OPENAI_API_TIMEOUT", 30)
    log.debug(f"API Timeout set to {api_timeout}")

    chat_save_perfix = config.get("CHAT_SAVE_PERFIX", "./chat_history_")

    chat_gpt = ChatGPT(api_key, api_timeout)
    
    if config.get("OPENAI_HOST"):
        chat_gpt.set_host(config.get("OPENAI_HOST"))


    if config.get("OPENAI_MODEL"):
        chat_gpt.set_model(config.get("OPENAI_MODEL"))

    if not config.getboolean("AUTO_GENERATE_TITLE", True):
        chat_gpt.auto_gen_title_background_enable = False
        log.debug("Auto title generation [bright_red]disabled[/]")

    gen_title_daemon_thread = threading.Thread(
        target=chat_gpt.auto_gen_title_background, daemon=True)
    gen_title_daemon_thread.start()
    log.debug("Title generation daemon thread started")

    if args.host:
        chat_gpt.set_host(args.host)
        console.print(_("gpt_term.host_set", new_host=args.host))

    # Custom command completion to ensure completion continues after typing '/'
    command_completer = CommandCompleter(chat_gpt)

    if args.model:
        chat_gpt.set_model(args.model)

    if args.multi:
        ChatMode.toggle_multi_line_mode()

    if args.raw:
        ChatMode.toggle_raw_mode()

    if args.load:
        chat_history = load_chat_history(args.load)
        if chat_history:
            change_CLI_title(args.load.rstrip(".json"))
            chat_gpt.messages = chat_history
            for message in chat_gpt.messages:
                print_message(message)
            chat_gpt.current_tokens = count_token(chat_gpt.messages)
            log.info(f"Chat history successfully loaded from: {args.load}")
            console.print(
                _("gpt_term.load_chat_history",load=args.load), highlight=False)
            
    if args.query:
        query_text = " ".join(args.query)
        log.info(f"> {query_text}")
        is_stdout_tty = os.isatty(sys.stdout.fileno())
        if is_stdout_tty:
            chat_gpt.handle(query_text)
        else:  # Running in pipe/stream mode
            chat_gpt.handle_simple(query_text)
        return
    else:
        console.print(_("gpt_term.welcome"))

    session = PromptSession()

    # Bind Enter event to achieve custom multi-line mode effect
    key_bindings = create_key_bindings()

    while True:
        try:
            _host = chat_gpt.host.split('//')[1]
            message = session.prompt(
                f"\n{_host} --> {chat_gpt.model}\n > ",
                completer=command_completer,
                complete_while_typing=True,
                key_bindings=key_bindings)

            if message.startswith('/'):
                command = message.strip()
                handle_command(command, chat_gpt,
                               key_bindings, chat_save_perfix, command_completer)
            else:
                if not message:
                    continue

                log.info(f"> {message}")
                chat_gpt.handle(message)

                if message.lower() in ['再见', 'bye', 'goodbye', '结束', 'end', '退出', 'exit', 'quit']:
                    break

        except KeyboardInterrupt:
            continue
        except EOFError:
            console.print(_("gpt_term.exit"))
            break

    log.info(f"Total tokens spent: {chat_gpt.total_tokens_spent}")
    console.print(
        _("gpt_term.spent_token",total_tokens_spent=chat_gpt.total_tokens_spent))
    
    threadlock_remote_version.acquire()
    if remote_version and remote_version > local_version:
        console.print(Panel(Group(
            Markdown(_("gpt_term.upgrade_use_command")),
            Markdown(_("gpt_term.upgrade_see_git"))),
            title=_("gpt_term.upgrade_title",local_version=str(local_version),remote_version=str(remote_version)),
            width=58, style="blue", title_align="left"))
    threadlock_remote_version.release()

if __name__ == "__main__":
    main()
