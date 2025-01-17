import logging
import threading
import time
import tkinter as tk
from dataclasses import dataclass, fields
from tkinter import ttk
from typing import Tuple, Dict, Any, Union, TypeVar

import numpy as np

import qdlutils.datagenerators.spectrometers.andor as andor
from qdlutils.hardware.spectrometers.utils import (
    make_label_and_entry,
    make_label_and_option_menu,
    make_label_and_check_button,
    make_label_frame,
    make_tab_view,
    make_popup_window_and_take_threaded_action,
    prepare_list_for_option_menu,
)

_TkVarType = TypeVar('_TkVarType', tk.Variable, tk.IntVar, tk.DoubleVar, tk.BooleanVar, tk.StringVar)


class AndorSpectrometerController:
    """
    This class is the controller for the Andor Spectrometer.
    It is responsible for handling the configuration and
    data acquisition of the Andor Spectrometer.
    The spectrometer configuration can be changed via a
    pop-up window, which retains information between closures.

    Attributes
    ----------
    logger: logging.Logger
        The logger object related to the Andor Spectrometer Controller.
    spectrometer_config: andor.AndorSpectrometerConfig
        The configuration object for the Andor Spectrometer.
    spectrometer_daq: andor.AndorSpectrometerDataAcquisition
        The data acquisition object for the Andor Spectrometer.
    last_config_dict: Dict[str, Any]
        The most recent configuration dictionary used for the Andor Spectrometer.
    last_measured_spectrum: np.ndarray
        The most recent measured spectrum.
    last_wavelength_array: np.ndarray
        The most recent measured wavelength array.
    config_view: Union[ConfigurationView, None]
        The configuration window pop-up.
    """

    def __init__(self, logger_level: int):
        """
        Parameters
        ----------
        logger_level: int
            The logging level for the Andor Spectrometer Controller.
        """
        self.logger = logging.getLogger('Andor Spectrometer Controller')
        self.logger.setLevel(logger_level)

        self.spectrometer_config = andor.AndorSpectrometerConfig(logger_level)

        # Changing supported acquisition modes to remove
        # "run until abort" which is nonsensical for this application.
        self.spectrometer_config.SUPPORTED_ACQUISITION_MODES = (
            self.spectrometer_config.AcquisitionMode.SINGLE_SCAN.name,
            self.spectrometer_config.AcquisitionMode.ACCUMULATE.name,
            self.spectrometer_config.AcquisitionMode.KINETICS.name,
        )
        self.spectrometer_daq = andor.AndorSpectrometerDataAcquisition(
            logger_level, self.spectrometer_config)

        self.last_config_dict = {}

        self.last_measured_spectrum = None
        self.last_wavelength_array = None

        self.config_view: Union[ConfigurationView, None] = None

    @property
    def clock_rate(self) -> float:
        """
        The clock rate of a single exposure (1/exposure_time in Hz).
        Strangely, this is saved in the npz data file, instead of the
        entire settings.

        This will have to do for now.
        """
        exposure_time = self.last_config_dict.get('exposure_time', np.nan)
        if self.last_config_dict.get('acquisition_mode', None) == \
                self.spectrometer_config.AcquisitionMode.ACCUMULATE.name:
            exposure_time *= self.last_config_dict.get('number_of_accumulations', np.nan)
        elif self.last_config_dict.get('acquisition_mode', None) == \
                self.spectrometer_config.AcquisitionMode.KINETICS.name:
            exposure_time *= self.last_config_dict.get('number_of_accumulations', np.nan)
            exposure_time *= self.last_config_dict.get('number_of_kinetics', np.nan)
        return 1 / exposure_time if exposure_time > 0 else np.nan

    def start(self) -> None:
        """
        This method is run by the Application's DAQ controller
        once before the actual acquisition sequence begins.
        For this Spectrometer controller, we decide that the
        connection should only be established during scans and
        updating settings.
        Hence, we connect to the spectrometer, and wait until
        the target temperature is reached (if the setting is on).

        Raises
        ------
        RuntimeError
            If the connection to the spectrometer fails.
            This is not a fatal error since the scanning thread will catch it,
            hence preventing the acquisition sequence from starting.
        """
        self.logger.info('Starting controller.')
        self.open()
        if not self.spectrometer_config.is_open:
            raise RuntimeError('Failed to connect to Andor Spectrometer.')
        self.spectrometer_daq.wait_for_target_temperature_if_necessary()

    def stop(self) -> None:
        """
        This method runs at the end of the acquisition sequence loop
        on the Application's DAQ controller.

        We do not abort the acquisition because when stop is on the
        Application's DAQ controller, it will change the `self.running`
        parameter to False but will keep taking data until the
        currently scanned row finishes.
        So, we only need to abort acquisition when the controller closes,
        in case it closes abruptly.
        """
        self.logger.info('Stopping controller.')
        self.spectrometer_daq.stop_waiting_to_reach_temperature()
        self.close()

    def open(self) -> bool:
        """
        Attempts to establish a connection to the spectrometer.

        The device will not save its settings when it closes,
        so every time we open the connection, we should load
        the previous configuration settings.

        If this method is called

        Returns
        -------
        bool
            True if the connection was successful, False otherwise.
        """
        self.logger.info('Opening Andor Spectrometer')
        self.spectrometer_config.open()
        connection_status: bool = self.spectrometer_config.is_open
        if connection_status:
            self.logger.info('Opening Andor Spectrometer was successful.')
            if self.last_config_dict:
                self.configure(self.last_config_dict.copy(), attempt_connection=False)
            self.logger.info('Latest configuration settings were set.')
        else:
            self.logger.info('Opening Andor Spectrometer failed.')
        return connection_status

    def close(self) -> bool:
        """
        Attempts to close the connection to the spectrometer.

        Returns
        -------
        bool
            True if the disconnection was successful, False otherwise.
        """
        if not self.spectrometer_config.is_open:
            self.logger.info('Andor Spectrometer is already closed.')
            return True
        self.logger.info('Closing Andor Spectrometer')
        self.spectrometer_daq.close()
        self.spectrometer_config.close()
        connection_status: bool = not self.spectrometer_config.is_open
        if connection_status:
            self.logger.info('Closing Andor Spectrometer was successful.')
        else:
            self.logger.info('Closing Andor Spectrometer failed.')
        return connection_status

    def _open_in_thread_and_wait_in_main(self, gui_root: tk.Toplevel):
        """
        Opens the spectrometer in a thread and waits for it to finish initializing.

        Parameters
        ----------
        gui_root : tk.Toplevel
            The root window of the GUI.

        Returns
        -------
        bool
            True if the connection was successful, False otherwise.
        """
        title = 'Connecting...'
        message = 'Connecting to Andor Spectrometer. Please wait...'
        # Wait for spectrometer initialization in a thread.
        # This will allow us to block the main GUI with a pop-up
        # window in the meantime - and helps prevent GUI freezes.

        thread_finished_event = threading.Event()
        make_popup_window_and_take_threaded_action(
            gui_root, title, message, self.open, end_event=thread_finished_event)

        time_start = time.time()
        while not thread_finished_event.is_set() and time.time() - time_start < 30:
            time.sleep(0.1)
        if not self.spectrometer_config.is_open:
            if not thread_finished_event.is_set():
                self.logger.error("Opening the spectrometer in a thread took too long.")
            else:
                self.logger.error("Failed to connect to spectrometer.")
            return False
        return True

    def sample_spectrum(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        This method is used in the Application's DAQ controller
        to collect a single batch of data.

        The data is collected from the spectrometer DAQ, and the
        wavelengths are calculated from the spectrometer's
        wavelength calibration.
        When acquiring single and accumulation mode scans, the
        data and wavelengths have the same shape.
        If kinetic series mode is used, the data have an extra
        dimension for each acquired spectrum in the series.
        Wavelengths are pixel and wavelength offset-corrected.

        Returns
        -------
        Tuple[np.ndarray, np.ndarray]
            A tuple containing the measured spectrum and the wavelength array.
        """
        self.logger.debug('Sampling Spectrum')
        acq_mode = self.spectrometer_config.acquisition_mode
        if acq_mode == self.spectrometer_config.AcquisitionMode.SINGLE_SCAN.name:
            return self.spectrometer_daq.acquire('single')
        elif acq_mode == self.spectrometer_config.AcquisitionMode.ACCUMULATE.name:
            return self.spectrometer_daq.acquire('accumulation')
        elif acq_mode == self.spectrometer_config.AcquisitionMode.KINETICS.name:
            return self.spectrometer_daq.acquire('kinetic series')

    def configure(self, config_dict: dict, attempt_connection: bool = True) -> None:
        """
        Configures the spectrometer with the provided settings.

        This method is used for two main reasons.
        The first is to set the initial yaml file configurations loaded
        through the Application's DAQ controller as a dictionary.
        The second is to set the spectrometer settings after the
        spectrometer controller class has been instantiated.

        During the first instantiation, the last_config_dict will be empty,
        and the spectrometer will not be connected, so we need to connect
        it first.
        Afterward, a disconnected spectrometer means there is a
        connection error, since the spectrometer is connected in the
        config window realization (see `configure_view` method below).

        When we set some of these values, Andor actually updates them
        according to system specifications.
        Hence, it is important to update the last_config_dict with the values
        Andor select.
        Beware, that updating these does not update the GUI!
        You would have to close and re-open the GUI to see the changes.

        Parameters
        ----------
        config_dict: dict
            A dictionary containing the configuration settings.
        attempt_connection: bool
            Will attempt to connect to the spectrometer and then disconnect.
            This is useful for when the configuration is set in the
            Application's DAQ controller.
            Default is True.
        """
        self.logger.setLevel(logging.DEBUG)
        self.logger.debug("Calling configure on the Andor spectrometer controller")

        if not self.last_config_dict:  # Expected to run during the first instantiation.
            self.logger.debug("First instantiation of the Andor Spectrometer controller. "
                              "Establishing connection now...")
            self.open()
        elif attempt_connection:
            self.logger.debug("Subsequent call for configuration outside of configuration GUI. "
                              "Establishing connection now...")
            self.open()

        # The spectrometer should already be open at this point, even
        # if this method is accessed via the set button of the config window.
        if not self.spectrometer_config.is_open:
            self.last_config_dict.update(config_dict)  # storing the input anyway!
            self.logger.error("Spectrometer is not open. Cannot configure.")
            return

        for key in self.last_config_dict:
            # catch non-existent keys, and values that are None at the same time
            if config_dict.get(key, None) is None:
                config_dict[key] = self.last_config_dict[key]

        # Device Settings
        ccd_value = config_dict['ccd_device_index']
        self.spectrometer_config.ccd_device_index = int(ccd_value) if ccd_value is not None else -1
        spg_value = config_dict['spg_device_index']
        self.spectrometer_config.spg_device_index = int(spg_value) if spg_value is not None else -1

        # Spectrograph Settings
        self.spectrometer_config.current_grating = config_dict['grating']
        self.spectrometer_config.center_wavelength = config_dict['center_wavelength']
        # -------------------------------
        self.spectrometer_config.pixel_offset = config_dict['pixel_offset']
        self.spectrometer_config.wavelength_offset = config_dict['wavelength_offset']
        # -------------------------------
        self.spectrometer_config.input_port = config_dict['input_port']
        self.spectrometer_config.output_port = config_dict['output_port']

        # Acquisition Settings
        self.spectrometer_config.read_mode = config_dict['read_mode']
        self.spectrometer_config.acquisition_mode = config_dict['acquisition_mode']
        self.spectrometer_config.trigger_mode = config_dict['trigger_mode']
        # -------------------------------
        self.spectrometer_config.exposure_time = config_dict['exposure_time']
        self.spectrometer_config.number_of_accumulations = config_dict['number_of_accumulations']
        self.spectrometer_config.accumulation_cycle_time = config_dict['accumulation_cycle_time']
        self.spectrometer_config.number_of_kinetics = config_dict['number_of_kinetics']
        self.spectrometer_config.kinetic_cycle_time = config_dict['kinetic_cycle_time']
        # -------------------------------
        self.spectrometer_config.baseline_clamp = config_dict['baseline_clamp']
        self.spectrometer_config.remove_cosmic_rays = config_dict['cosmic_ray_removal']
        self.spectrometer_config.keep_clean_on_external_trigger = config_dict['keep_clean_on_external_trigger']
        # -------------------------------
        single_track_center_row = config_dict['single_track_center_row']
        single_track_height = config_dict['single_track_height']
        self.spectrometer_config.single_track_read_mode_parameters = \
            andor.SingleTrackReadModeParameters(single_track_center_row, single_track_height)

        # Electronics Settings
        vss_value = config_dict['vertical_shift_speed']
        if isinstance(vss_value, str):  # Handles None
            vss_value = float(vss_value)
        self.spectrometer_config.vertical_shift_speed = vss_value

        hss_value = config_dict['horizontal_shift_speed']
        if hss_value is not None:
            hss_value = hss_value[1:-1].replace(' ', '').split(',')
            self.spectrometer_config.ad_channel = int(hss_value[0])
            self.spectrometer_config.output_amplifier = int(hss_value[1])
            self.spectrometer_config.horizontal_shift_speed = float(hss_value[2])

        pre_amp_gain_value = config_dict['pre_amp_gain']
        if pre_amp_gain_value is not None:
            self.spectrometer_config.pre_amp_gain = float(pre_amp_gain_value)

        # Temperature Settings
        self.spectrometer_config.sensor_temperature_set_point = config_dict['target_sensor_temperature']
        self.spectrometer_daq.reach_temperature_before_acquisition = config_dict['reach_temperature_before_acquisition']
        # -------------------------------
        self.spectrometer_config.cooler = config_dict['cooler']
        self.spectrometer_config.cooler_persistence_mode = config_dict['cooler_persistence']

        # update last_config_dict with actual values in the gui
        self.update_config_dict_from_current_values()

        if attempt_connection:
            self.close()

    def update_config_dict_from_current_values(self):
        self.last_config_dict['ccd_device_index'] = str(self.spectrometer_config.ccd_device_index)
        self.last_config_dict['spg_device_index'] = str(self.spectrometer_config.spg_device_index)

        self.last_config_dict['grating'] = self.spectrometer_config.current_grating
        self.last_config_dict['center_wavelength'] = self.spectrometer_config.center_wavelength

        self.last_config_dict['pixel_offset'] = self.spectrometer_config.pixel_offset
        self.last_config_dict['wavelength_offset'] = self.spectrometer_config.wavelength_offset

        self.last_config_dict['input_port'] = self.spectrometer_config.input_port
        self.last_config_dict['output_port'] = self.spectrometer_config.output_port

        self.last_config_dict['read_mode'] = self.spectrometer_config.read_mode
        self.last_config_dict['acquisition_mode'] = self.spectrometer_config.acquisition_mode
        self.last_config_dict['trigger_mode'] = self.spectrometer_config.trigger_mode

        self.last_config_dict['exposure_time'] = self.spectrometer_config.exposure_time
        self.last_config_dict['number_of_accumulations'] = self.spectrometer_config.number_of_accumulations
        self.last_config_dict['accumulation_cycle_time'] = self.spectrometer_config.accumulation_cycle_time
        self.last_config_dict['number_of_kinetics'] = self.spectrometer_config.number_of_kinetics
        self.last_config_dict['kinetic_cycle_time'] = self.spectrometer_config.kinetic_cycle_time

        self.last_config_dict['baseline_clamp'] = self.spectrometer_config.baseline_clamp
        self.last_config_dict['cosmic_ray_removal'] = self.spectrometer_config.remove_cosmic_rays
        self.last_config_dict['keep_clean_on_external_trigger'] = \
            self.spectrometer_config.keep_clean_on_external_trigger

        self.last_config_dict['single_track_center_row'] = \
            self.spectrometer_config.single_track_read_mode_parameters.track_center_row
        self.last_config_dict['single_track_height'] = \
            self.spectrometer_config.single_track_read_mode_parameters.track_height

        self.last_config_dict['vertical_shift_speed'] = str(self.spectrometer_config.vertical_shift_speed)
        self.last_config_dict['horizontal_shift_speed'] = str((
            self.spectrometer_config.ad_channel,
            self.spectrometer_config.output_amplifier,
            self.spectrometer_config.horizontal_shift_speed
        ))
        self.last_config_dict['pre_amp_gain'] = str(self.spectrometer_config.pre_amp_gain)

        self.last_config_dict['target_sensor_temperature'] = self.spectrometer_config.sensor_temperature_set_point
        self.last_config_dict['reach_temperature_before_acquisition'] = \
            self.spectrometer_daq.reach_temperature_before_acquisition
        self.last_config_dict['cooler'] = self.spectrometer_config.cooler
        self.last_config_dict['cooler_persistence'] = self.spectrometer_config.cooler_persistence_mode

    def configure_view(self, gui_root: tk.Toplevel) -> None:
        """
        Launch a window to configure the spectrometer after its
        first instantiation.

        Parameters
        ----------
        gui_root : tk.Toplevel
            The root window of the GUI.
            This is used to create the new window as a child widget.
        """
        if not self.spectrometer_config.is_open:
            self.logger.info("Spectrometer is not open. Opening it.")
            successful_connection = self._open_in_thread_and_wait_in_main(gui_root)
            # if not successful_connection:
            #     self.logger.error("Aborting configuration window creation.")
            #     return

        if self.config_view is None:
            self.config_view = ConfigurationView(gui_root, self)

        self.config_view.show()

    def print_config(self) -> None:
        """
        Prints the current spectrometer configuration to the console.
        """
        print("Andor spectrometer config")
        print("-------------------------")
        for key in self.last_config_dict:
            print(key, ':', self.last_config_dict[key])
        print("-------------------------")

    def __del__(self):
        self.close()


@dataclass
class AndorSpectrometerConfigDataVariables:
    """
    Dataclass to hold and update configuration
    GUI variables and spectrometer parameters
    via the Andor spectrometer controller GUI.
    """
    logger: logging.Logger

    # Devices
    # - Device Index
    ccd_device_index: tk.StringVar
    spg_device_index: tk.StringVar

    # Spectrograph
    # - Turret
    grating: tk.StringVar
    center_wavelength: tk.DoubleVar

    # - Calibration
    pixel_offset: tk.DoubleVar
    wavelength_offset: tk.DoubleVar
    # - Ports
    input_port: tk.StringVar
    output_port: tk.StringVar

    # Acquisition
    # - Modes
    read_mode: tk.StringVar
    acquisition_mode: tk.StringVar
    trigger_mode: tk.StringVar
    # - Timing
    exposure_time: tk.DoubleVar
    number_of_accumulations: tk.IntVar
    accumulation_cycle_time: tk.DoubleVar
    number_of_kinetics: tk.IntVar
    kinetic_cycle_time: tk.DoubleVar
    #  - Data-Pre-Processing
    baseline_clamp: tk.BooleanVar
    cosmic_ray_removal: tk.BooleanVar
    keep_clean_on_external_trigger: tk.BooleanVar
    # - Single Track Setup
    single_track_center_row: tk.IntVar
    single_track_height: tk.IntVar

    # Electronics
    # - Vertical Shift
    vertical_shift_speed: tk.StringVar
    # - Horizontal Shift
    horizontal_shift_speed: tk.StringVar
    pre_amp_gain: tk.StringVar

    # Temperature
    # - Set Point
    target_sensor_temperature: tk.IntVar
    reach_temperature_before_acquisition: tk.BooleanVar
    # - Cooler
    cooler: tk.BooleanVar
    cooler_persistence: tk.BooleanVar

    def update_variables_from_dict(self, config_dict: Dict[str, Any]) -> None:
        """
        Updates the variables from the given configuration dictionary.

        Parameters
        ----------
        config_dict: Dict[str, float]
            The configuration dictionary.
        """
        for key in config_dict.keys():
            if hasattr(self, key):
                variable: tk.Variable = getattr(self, key)
                variable.set(config_dict[key])
            else:
                message = (f"Unknown key '{key}' was passed in Andor "
                           f"Spectrometer configuration dictionary.")
                self.logger.warning(message)

    def get_config_dict(self) -> Dict[str, Any]:
        """
        Returns the configuration dictionary.

        Returns
        -------
        Dict[str, Any]
            The configuration dictionary.
        """
        variable_keys = [f.name for f in fields(self) if isinstance(getattr(self, f.name), tk.Variable)]

        config_dict = {}
        for key in variable_keys:
            variable: _TkVarType = getattr(self, key)
            config_dict[key] = variable.get()

        return config_dict

    def variable_dict(self) -> Dict[str, _TkVarType]:
        """
        Returns a dictionary of the GUI variables

        Returns
        -------
        Dict[str, _TkVarType]
            A dictionary of the GUI variables.
        """
        variable_keys = [f.name for f in fields(self) if isinstance(getattr(self, f.name), tk.Variable)]

        var_dict = {}
        for key in variable_keys:
            var_dict[key] = getattr(self, key)

        return var_dict


class ConfigurationView:
    """
    A class to create a configuration window pop-up for
    the Andor Spectrometer Controller.
    The window consists of multiple tabs, each corresponding
    to different settings and two buttons Set and Close.

    When the parameters are set, the window updates
    with the most recent parameters, since Andor
    changes the parameter values if they are not permitted.
    For example, if the accumulation cycle time was set to
    a value shorter than the exposure time, the
    accumulation time would be updated to at least the
    same value.

    When the window is closed, the configuration
    is stored in the background, and will be used
    when the window reopens.
    Hence, this class is designed to be instantiated once,
    and called upon in subsequent requests for the gui.
    """

    def __init__(
            self,
            gui_root: tk.Toplevel,
            controller: AndorSpectrometerController,
    ):
        """
        Parameters
        ----------
        gui_root : tk.Toplevel
            The root window of the GUI.
            This is used to create the new window as a child widget.
        controller : AndorSpectrometerController
            The AndorSpectrometerController object.
        """
        self.spectrometer_controller = controller
        self.spectrometer_config = self.spectrometer_controller.spectrometer_config
        self.logger = self.spectrometer_controller.logger

        self._create_view(gui_root)

    def _create_view(self, gui_root: tk.Toplevel):
        """
        Creates the configuration view.

        Parameters
        ----------
        gui_root : tk.Toplevel
            The root window of the GUI.
            This is used to create the new window as a child widget.
        """
        config_dict = self.spectrometer_controller.last_config_dict

        self.logger.debug('Creating configuration window.')
        self.config_win = tk.Toplevel(gui_root)
        self.config_win.protocol('WM_DELETE_WINDOW', self._on_close_click)
        self.config_win.grab_set()
        self.config_win.title('Andor Spectrometer Settings')
        label_padx = 10

        tab_view = make_tab_view(self.config_win, tab_pady=0)

        device_tab = ttk.Frame(tab_view)
        spectrograph_tab = ttk.Frame(tab_view)
        acquisition_tab = ttk.Frame(tab_view)
        electronics_tab = ttk.Frame(tab_view)
        temperature_tab = ttk.Frame(tab_view)

        tab_view.add(device_tab, text='Devices')
        tab_view.add(spectrograph_tab, text='Spectrograph')
        tab_view.add(acquisition_tab, text='Acquisition')
        tab_view.add(electronics_tab, text='Electronics')
        tab_view.add(temperature_tab, text='Temperature')

        # Device Settings
        row = 0
        device_settings_frame = make_label_frame(device_tab, 'Device Index', row)

        frame_row = 0
        ccd_device_list = prepare_list_for_option_menu(self.spectrometer_config.ccd_device_list)
        ccd_device_value = str(config_dict['ccd_device_index'])
        ccd_device_value = ccd_device_value if ccd_device_value in ccd_device_list else 'None'
        _, _, ccd_device_index_var = make_label_and_option_menu(
            device_settings_frame, 'CCD', frame_row,
            ccd_device_list, ccd_device_value, label_padx)

        frame_row += 1
        spg_device_list = prepare_list_for_option_menu(self.spectrometer_config.spg_device_list)
        spg_device_value = str(config_dict['spg_device_index'])
        spg_device_value = spg_device_value if spg_device_value in spg_device_list else 'None'
        _, _, spg_device_index_var = make_label_and_option_menu(
            device_settings_frame, 'Spectrograph', frame_row,
            spg_device_list, spg_device_value, label_padx)

        # Spectrograph Settings
        row = 0
        turret_frame = make_label_frame(spectrograph_tab, 'Turret', row)

        frame_row = 0
        grating_list = prepare_list_for_option_menu(self.spectrometer_config.grating_list)
        _, _, grating_var = make_label_and_option_menu(
            turret_frame, 'Grating (Idx: Grooves, Blaze)', frame_row,
            grating_list, config_dict['grating'], label_padx)

        frame_row += 1
        _, _, center_wavelength_var = make_label_and_entry(
            turret_frame, 'Center Wavelength (nm)', frame_row,
            config_dict['center_wavelength'], tk.DoubleVar, label_padx)

        row += 1
        calibration_frame = make_label_frame(spectrograph_tab, 'Calibration', row)

        frame_row = 0
        _, _, pixel_offset_var = make_label_and_entry(
            calibration_frame, 'Pixel Offset', frame_row,
            config_dict['pixel_offset'], tk.DoubleVar, label_padx)

        frame_row += 1
        _, _, wavelength_offset_var = make_label_and_entry(
            calibration_frame, 'Wavelength Offset (nm)', frame_row,
            config_dict['wavelength_offset'], tk.DoubleVar, label_padx)

        row += 1
        port_frame = make_label_frame(spectrograph_tab, 'Ports', row)

        frame_row = 0
        flipper_mirror_list = self.spectrometer_config.SpectrographFlipperMirrorPort._member_names_
        _, _, input_port_var = make_label_and_option_menu(
            port_frame, 'Input', frame_row,
            flipper_mirror_list, config_dict['input_port'], label_padx)

        frame_row += 1
        _, _, output_port_var = make_label_and_option_menu(
            port_frame, 'Output', frame_row,
            flipper_mirror_list, config_dict['output_port'], label_padx)

        # Acquisition Settings
        row = 0
        modes_frame = make_label_frame(acquisition_tab, 'Modes', row)

        frame_row = 0
        _, _, read_mode_var = make_label_and_option_menu(
            modes_frame, 'Read', frame_row,
            self.spectrometer_config.SUPPORTED_READ_MODES, config_dict['read_mode'], label_padx)

        frame_row += 1
        _, _, acquisition_mode_var = make_label_and_option_menu(
            modes_frame, 'Acquisition', frame_row,
            self.spectrometer_config.SUPPORTED_ACQUISITION_MODES, config_dict['acquisition_mode'], label_padx)

        frame_row += 1
        _, _, trigger_mode_var = make_label_and_option_menu(
            modes_frame, 'Trigger', frame_row,
            self.spectrometer_config.SUPPORTED_TRIGGER_MODES, config_dict['trigger_mode'], label_padx)

        row += 1
        timing_frame = make_label_frame(acquisition_tab, 'Timing', row)

        frame_row = 0
        _, _, exposure_time_var = make_label_and_entry(
            timing_frame, 'Exposure (s)', frame_row,
            config_dict['exposure_time'], tk.DoubleVar, label_padx)

        frame_row += 1
        _, _, no_of_accumulations_var = make_label_and_entry(
            timing_frame, 'No. of Accumulations', frame_row,
            config_dict['number_of_accumulations'], tk.IntVar, label_padx)

        frame_row += 1
        _, _, accumulation_cycle_time_var = make_label_and_entry(
            timing_frame, 'Accumulation Cycle (s)', frame_row,
            config_dict['accumulation_cycle_time'], tk.DoubleVar, label_padx)

        frame_row += 1
        _, _, no_of_kinetics_var = make_label_and_entry(
            timing_frame, 'No. of Kinetics', frame_row,
            config_dict['number_of_kinetics'], tk.IntVar, label_padx)

        frame_row += 1
        _, _, kinetic_cycle_time_var = make_label_and_entry(
            timing_frame, 'Kinetic Cycle (s)', frame_row,
            config_dict['kinetic_cycle_time'], tk.DoubleVar, label_padx)

        row += 1
        data_pre_processing_frame = make_label_frame(acquisition_tab, 'Data Pre-processing', row)

        frame_row = 0
        _, _, baseline_clamp_var = make_label_and_check_button(
            data_pre_processing_frame, 'Clamp Baseline', frame_row,
            config_dict['baseline_clamp'], label_padx)

        frame_row += 1
        _, _, cosmic_ray_removal_var = make_label_and_check_button(
            data_pre_processing_frame, 'Cosmic Ray Removal', frame_row,
            config_dict['cosmic_ray_removal'], label_padx)

        frame_row += 1
        _, _, keep_clean_on_external_trigger_var = make_label_and_check_button(
            data_pre_processing_frame, 'Keep Clean on Ext. Trigger', frame_row,
            config_dict['keep_clean_on_external_trigger'], label_padx)

        row += 1
        single_track_mode_frame = make_label_frame(acquisition_tab, 'Single Track Setup', row)

        frame_row = 0
        label_text = f'Center Row [1, {self.spectrometer_config.ccd_info.number_of_pixels_vertically}]'
        _, _, single_track_center_row_var = make_label_and_entry(
            single_track_mode_frame, label_text, frame_row,
            config_dict['single_track_center_row'], tk.IntVar, label_padx)

        frame_row += 1
        _, _, single_track_height_var = make_label_and_entry(
            single_track_mode_frame, 'Height', frame_row,
            config_dict['single_track_height'], tk.IntVar, label_padx)

        # Electronics Settings
        row = 0
        vertical_shift_frame = make_label_frame(electronics_tab, 'Vertical Shift', row)

        frame_row = 0
        vertical_shift_speed_options = prepare_list_for_option_menu(
            self.spectrometer_config.ccd_info.available_vertical_shift_speeds)
        vss_value = str(config_dict['vertical_shift_speed'])
        vss_value = vss_value if vss_value in vertical_shift_speed_options else 'None'
        _, _, vertical_speed_var = make_label_and_option_menu(
            vertical_shift_frame, 'Speed (μs)', frame_row,
            vertical_shift_speed_options, vss_value, label_padx)

        row += 1
        horizontal_shift_frame = make_label_frame(electronics_tab, 'Horizontal Shift', row)

        frame_row = 0
        hss_list = [(ad, amp, hss)
                    for ad, amp in self.spectrometer_config.ccd_info.available_horizontal_shift_speeds
                    for hss in self.spectrometer_config.ccd_info.available_horizontal_shift_speeds[(ad, amp)]]
        horizontal_shift_speed_options = prepare_list_for_option_menu(hss_list)
        hss_value = str(config_dict['horizontal_shift_speed'])
        hss_value = hss_value if hss_value in horizontal_shift_speed_options else 'None'
        _, _, horizontal_speed_var = make_label_and_option_menu(
            horizontal_shift_frame, '       A/D Channel\n   Output Amplifier\nReadout Rate (MHz)', frame_row,
            horizontal_shift_speed_options, hss_value, label_padx)

        frame_row += 1
        pre_amp_gain_list = prepare_list_for_option_menu(
            self.spectrometer_config.ccd_info.available_pre_amp_gains)
        pre_amp_gain_value = str(config_dict['pre_amp_gain'])
        pre_amp_gain_value = pre_amp_gain_value if pre_amp_gain_value in pre_amp_gain_list else 'None'
        _, _, pre_amp_gain_var = make_label_and_option_menu(
            horizontal_shift_frame, 'Pre-Amplifier Gain', frame_row,
            pre_amp_gain_list, pre_amp_gain_value, label_padx)

        # Temperature Settings
        row = 0
        temperature_set_point_frame = make_label_frame(temperature_tab, 'Set Point', row)

        frame_row = 0
        _, _, target_sensor_temperature_var = make_label_and_entry(
            temperature_set_point_frame, 'Temperature (°C)', frame_row,
            config_dict['target_sensor_temperature'], tk.IntVar, label_padx)

        frame_row += 1
        _, _, reach_temperature_before_acq_var = make_label_and_check_button(
            temperature_set_point_frame, 'Reach before Acquisition', frame_row,
            config_dict['reach_temperature_before_acquisition'], label_padx)

        row += 1
        cooler_frame = make_label_frame(temperature_tab, 'Cooler', row)

        frame_row = 0
        _, _, is_cooling_var = make_label_and_check_button(
            cooler_frame, 'Cooling', frame_row,
            config_dict['cooler'], label_padx)

        frame_row += 1
        _, _, cooler_persistence_var = make_label_and_check_button(
            cooler_frame, 'Persistent Cooling', frame_row,
            config_dict['cooler_persistence'], label_padx)

        # Pack variables into a dictionary to pass to the _set_from_gui method
        self.config_data_variables = AndorSpectrometerConfigDataVariables(
            self.logger,
            # Devices
            # - Device Index
            ccd_device_index=ccd_device_index_var,
            spg_device_index=spg_device_index_var,
            # Spectrograph
            # - Turret
            grating=grating_var,
            center_wavelength=center_wavelength_var,
            # - Calibration
            pixel_offset=pixel_offset_var,
            wavelength_offset=wavelength_offset_var,
            # - Ports
            input_port=input_port_var,
            output_port=output_port_var,
            # Acquisition
            # - Modes
            read_mode=read_mode_var,
            acquisition_mode=acquisition_mode_var,
            trigger_mode=trigger_mode_var,
            # - Timing
            exposure_time=exposure_time_var,
            number_of_accumulations=no_of_accumulations_var,
            accumulation_cycle_time=accumulation_cycle_time_var,
            number_of_kinetics=no_of_kinetics_var,
            kinetic_cycle_time=kinetic_cycle_time_var,
            # - Data-Pre-Processing
            baseline_clamp=baseline_clamp_var,
            cosmic_ray_removal=cosmic_ray_removal_var,
            keep_clean_on_external_trigger=keep_clean_on_external_trigger_var,
            # - Single Track Setup
            single_track_center_row=single_track_center_row_var,
            single_track_height=single_track_height_var,
            # Electronics
            # - Vertical Shift
            vertical_shift_speed=vertical_speed_var,
            # - Horizontal Shift
            horizontal_shift_speed=horizontal_speed_var,
            pre_amp_gain=pre_amp_gain_var,
            # Temperature
            # - Set Point
            target_sensor_temperature=target_sensor_temperature_var,
            reach_temperature_before_acquisition=reach_temperature_before_acq_var,
            # - Cooler
            cooler=is_cooling_var,
            cooler_persistence=cooler_persistence_var
        )

        row = 1
        set_button = ttk.Button(self.config_win, text='Set', command=self._on_set_click)
        set_button.grid(row=row, column=0, pady=5)

        close_button = ttk.Button(self.config_win, text='Close', command=self._on_close_click)
        close_button.grid(row=row, column=1, pady=5)

        tab_view.select(2)

        # Setting window geometry, so that it opens in the middle of the parent application
        self.config_win.update_idletasks()
        width = self.config_win.winfo_reqwidth()
        height = self.config_win.winfo_reqheight()
        x = gui_root.winfo_x() + gui_root.winfo_width() // 2 - width // 2
        y = gui_root.winfo_y() + gui_root.winfo_height() // 2 - height // 2
        self.config_win.geometry(f'{width}x{height}+{x}+{y}')

        self.logger.debug('Configuration window has been created.')

    def _on_close_click(self):
        """
        Closes the configuration window and closes the connection
        to the spectrometer.
        """
        self.logger.debug('Closing configuration window.')
        self.spectrometer_controller.close()
        self.hide()

    def _on_set_click(self):
        """
        Sets the new spectrometer configuration in a thread,
        while the main window is disabled showing a waiting
        message in a popup window.

        Notes
        -----
        Changing configuration in a spectrometer may take
        a while because a lot of its components are moving
        parts that take time to be set.
        Using a popup window in this manner prevents the GUI
        from freezing.
        """
        gui_info = self.config_data_variables.variable_dict()
        title = 'Loading...'
        message = 'Loading the new spectrometer configuration.\nPlease wait...'
        self.logger.info(f'Setting new spectrometer configuration in a thread.')
        make_popup_window_and_take_threaded_action(
            self.config_win, title, message, lambda: self._update_spectrometer_configuration(gui_info))

    def _update_spectrometer_configuration(self, gui_vars: Dict[str, _TkVarType]) -> None:
        """
        Sets the new spectrometer configuration from the
        configuration window variables, and update
        GUI with the values Andor actually set.

        Parameters
        ----------
        Dict[str, _TkVarType]
            A dictionary with keys the same as the ones appearing
            in the YAML configuration file, and `tk.Variable`s pointing
            to the corresponding spectrometer parameter.
        """
        config_dict = {k: v.get() if v.get() not in ['None', ''] else None
                       for k, v in gui_vars.items()}  # code to handle the edge case where there are "None" values
        self.logger.info(config_dict)
        self.spectrometer_controller.configure(config_dict, attempt_connection=False)
        self.config_data_variables.update_variables_from_dict(self.spectrometer_controller.last_config_dict)
        self.logger.info('Spectrometer configuration updated.')

    def show(self):
        """
        Updates the GUI variables to the most recent
        configuration settings, disables the parent
        window, and opens the config window.
        """
        self.logger.debug('Showing configuration window.')
        self.config_data_variables.update_variables_from_dict(self.spectrometer_controller.last_config_dict)
        self.config_win.grab_set()
        self.config_win.update()
        self.config_win.wm_deiconify()

    def hide(self):
        """
        Hides the configuration window.
        """
        self.logger.debug('Hiding configuration window.')
        self.config_win.withdraw()
        self.config_win.grab_release()
