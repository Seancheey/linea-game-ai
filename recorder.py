import os.path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event
from typing import List, Set

import keyboard
import numpy as np
import psutil
from cv2 import cv2
from rich.progress import Progress, TextColumn, TimeElapsedColumn

from helper.data_format import np_keys_filename, avi_video_filename, np_screens_filename, recording_keys, img_size
from helper.screen_streamer import ScreenStreamer
from helper.transforms import keys_to_directions
from helper.window_region import WindowRegion


@dataclass
class KeyEvent:
    key_code: str
    timestamp: float
    down: bool


@dataclass
class ScreenEvent:
    screen: np.array
    timestamp: float


@dataclass
class DatasetItem:
    screen: np.array
    key_codes: List[str]
    timestamp: float = 0


@dataclass
class Recorder:
    save_dir: str
    recording_keys: Set[str] = field(default_factory=lambda: set(recording_keys))
    finish_record_key: str = 'space'
    discard_tail_sec: float = 3  # discard last N seconds of content, so that failing movement won't be learnt by model.
    key_recording_delay_sec: float = -0.010  # record key events N sec earlier to compensate for delay
    screen_streamer: ScreenStreamer = field(default_factory=lambda: ScreenStreamer(
        max_fps=30,
        output_img_format=img_size,
        record_window_region=WindowRegion.from_first_monitor()
    ))

    def record(self):
        stop_event = Event()
        with ThreadPoolExecutor(3) as pool:
            screen_future = pool.submit(self.__record_screen, stop_event)
            keyboard_future = pool.submit(self.__record_keyboard, stop_event)
            pool.submit(self.__listen_to_finish_record_event, stop_event).result()

            screen_data = screen_future.result()
            keyboard_data = keyboard_future.result()

        dataset = self.__to_training_data(keyboard_data, screen_data)

        if len(dataset) == 0:
            print('skipping saving dataset due to empty content\n')
            return

        folder_name = datetime.now().strftime('%Y%m%d-%H%M%S')
        os.mkdir(os.path.join(self.save_dir, folder_name))
        self.__save_np_keys(dataset, folder_name)
        self.__save_np_screens(dataset, folder_name)
        self.__save_avi_video(dataset, folder_name)
        print(f'saved data to {folder_name}\n')

    def __listen_to_finish_record_event(self, stop_event):
        keyboard.wait(self.finish_record_key)
        stop_event.set()

    def __record_keyboard(self, stop_event: Event) -> List[KeyEvent]:
        key_sequence = []

        def handle_event(event: keyboard.KeyboardEvent):
            key_sequence.append(
                KeyEvent(event.name, datetime.now().timestamp() + self.key_recording_delay_sec,
                         event.event_type == 'down'))

        for key in self.recording_keys:
            keyboard.hook_key(key, handle_event)

        stop_event.wait()
        return key_sequence

    def __record_screen(self, stop_event: Event) -> List[ScreenEvent]:
        with Progress(
                TextColumn("Video Recorder Stats:"),
                TimeElapsedColumn(),
                TextColumn("[progress.description]{task.fields[fps]}")
        ) as progress:
            screens = []
            for img in self.screen_streamer.stream(stop_event, progress):
                screens.append(ScreenEvent(img, datetime.now().timestamp()))
        return screens

    def __to_training_data(self, key_sequence: List[KeyEvent], screen_sequence: List[ScreenEvent]):
        ki, si = 0, 0
        cur_keys = set()
        data_out = []
        end_timestamp = screen_sequence[-1].timestamp - self.discard_tail_sec
        while si < len(screen_sequence):
            key_event = key_sequence[ki] if ki < len(key_sequence) else None
            screen_event = screen_sequence[si]
            if key_event is not None and key_event.timestamp < screen_event.timestamp:
                if key_event.down:
                    cur_keys.add(key_event.key_code)
                else:
                    cur_keys.remove(key_event.key_code)
                ki += 1
            else:
                if screen_event.timestamp > end_timestamp:
                    break
                data_out.append(DatasetItem(
                    screen=screen_event.screen,
                    key_codes=list(cur_keys),
                    timestamp=screen_event.timestamp
                ))
                si += 1
        return data_out

    def __save_np_screens(self, dataset: List[DatasetItem], folder: str):
        np.save(os.path.join(self.save_dir, folder, np_screens_filename),
                np.stack(list(map(lambda x: x.screen, dataset))))

    def __save_np_keys(self, dataset: List[DatasetItem], folder: str):
        np.save(os.path.join(self.save_dir, folder, np_keys_filename),
                np.stack(list(map(lambda x: keys_to_directions(x.key_codes), dataset))))

    def __save_avi_video(self, dataset: List[DatasetItem], folder: str):
        avg_fps = len(dataset) / (dataset[-1].timestamp - dataset[0].timestamp)
        print('average fps =', round(avg_fps, 2))
        video_writer = cv2.VideoWriter(os.path.join(self.save_dir, folder, avi_video_filename),
                                       cv2.VideoWriter_fourcc(*"XVID"), avg_fps,
                                       self.screen_streamer.output_img_format.resolution_shape())
        for item in dataset:
            video_writer.write(item.screen)
        video_writer.release()


def main():
    start_key = 'e'
    stop_key = 'q'
    next_key = 'space'
    keyboard.add_hotkey(stop_key, lambda: psutil.Process(os.getpid()).terminate())
    print(f'press "{start_key}" to start recording.')
    keyboard.wait(start_key)
    print(f'start recording... (press "{stop_key}" to exit, press "{next_key}" to save and start next recording)')
    data_dir = os.path.join(os.getcwd(), 'data')
    if not os.path.exists(data_dir):
        os.mkdir(data_dir)
    while True:
        recorder = Recorder(save_dir=data_dir, finish_record_key=next_key)
        recorder.record()


if __name__ == "__main__":
    main()
