"""
"""
from pathlib import Path

import napari
import numpy as np

from napari_n2v.resources import ICON_JUGLAB
from napari_n2v.utils import (
    State,
    UpdateType,
    DENOISING,
    prediction_worker,
    loading_worker,
    reshape_napari,
    get_images_count,
    get_napari_shapes
)
from napari_n2v.widgets import (
    AxesWidget,
    FolderWidget,
    load_button,
    layer_choice, ScrollWidgetWrapper, BannerWidget, create_gpu_label, create_progressbar
)
from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QProgressBar,
    QCheckBox,
    QTabWidget,
    QGroupBox,
    QLabel
)

SAMPLE = 'Sample data'


class PredictWidgetWrapper(ScrollWidgetWrapper):
    def __init__(self, napari_viewer):
        super().__init__(PredictWidget(napari_viewer))


class PredictWidget(QWidget):
    def __init__(self, napari_viewer):
        super().__init__()

        self.state = State.IDLE
        self.viewer = napari_viewer

        self.setLayout(QVBoxLayout())
        self.setMinimumWidth(200)
        self.setMaximumHeight(620)

        ###############################

        # add banner
        self.layout().addWidget(BannerWidget('N2V - Prediction',
                                             ICON_JUGLAB,
                                             'A self-supervised denoising algorithm.',
                                             'https://github.com/juglab/napari-n2v',
                                             'https://github.com/juglab/napari-n2v'))

        # add GPU button
        gpu_button = create_gpu_label()
        gpu_button.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.layout().addWidget(gpu_button)

        # QTabs
        self.tabs = QTabWidget()
        tab_layers = QWidget()
        tab_layers.setLayout(QVBoxLayout())

        tab_disk = QWidget()
        tab_disk.setLayout(QVBoxLayout())

        # add tabs
        self.tabs.addTab(tab_layers, 'From layers')
        self.tabs.addTab(tab_disk, 'From disk')
        self.tabs.setMaximumHeight(120)

        # image layer tab
        self.images = layer_choice(annotation=napari.layers.Image, name="Images")
        tab_layers.layout().addWidget(self.images.native)

        # disk tab
        self.lazy_loading = QCheckBox('Lazy loading')
        tab_disk.layout().addWidget(self.lazy_loading)
        self.images_folder = FolderWidget('Choose')
        tab_disk.layout().addWidget(self.images_folder)

        # add to main layout
        self.layout().addWidget(self.tabs)
        self.images.choices = [x for x in napari_viewer.layers if type(x) is napari.layers.Image]

        ###############################
        self._build_params_widgets()

        # progress bar
        self._build_predict_widgets()

        # place holders
        self.worker = None
        self.denoi_prediction = None
        self.sample_image = None
        self.n_im = 0
        self.is_from_disk = False

        # actions
        self.tabs.currentChanged.connect(self._update_tab_axes)
        self.predict_button.clicked.connect(self._start_prediction)
        self.images.changed.connect(self._update_layer_axes)
        self.images_folder.text_field.textChanged.connect(self._update_disk_axes)
        self.enable_3d.stateChanged.connect(self._update_3D)

    def _build_params_widgets(self):
        self.params_group = QGroupBox()
        self.params_group.setTitle("Parameters")
        self.params_group.setLayout(QVBoxLayout())
        self.params_group.layout().setContentsMargins(20, 20, 20, 0)
        # load model button
        self.load_model_button = load_button()
        self.params_group.layout().addWidget(self.load_model_button.native)
        # load 3D enabling checkbox
        self.enable_3d = QCheckBox('Enable 3D')
        self.params_group.layout().addWidget(self.enable_3d)
        # axes widget
        self.axes_widget = AxesWidget()
        self.params_group.layout().addWidget(self.axes_widget)
        self.layout().addWidget(self.params_group)

    def _build_predict_widgets(self):
        self.predict_group = QGroupBox()
        self.predict_group.setTitle("Prediction")
        self.predict_group.setLayout(QVBoxLayout())
        self.predict_group.layout().setContentsMargins(20, 20, 20, 0)

        # prediction progress bar
        self.pb_prediction = create_progressbar(text_format=f'Prediction ?/?')
        self.pb_prediction.setToolTip('Show the progress of the prediction')

        # predict button
        predictions = QWidget()
        predictions.setLayout(QHBoxLayout())
        self.predict_button = QPushButton('Predict', self)
        self.predict_button.setEnabled(True)
        self.predict_button.setToolTip('Run the trained model on the images')

        predictions.layout().addWidget(QLabel(''))
        predictions.layout().addWidget(self.predict_button)

        # add to the group
        self.predict_group.layout().addWidget(self.pb_prediction)
        self.predict_group.layout().addWidget(predictions)
        self.layout().addWidget(self.predict_group)

    def _update_3D(self):
        self.axes_widget.update_is_3D(self.enable_3d.isChecked())
        self.axes_widget.set_text_field(self.axes_widget.get_default_text())

    def _update_layer_axes(self):
        if self.images.value is not None:
            shape = self.images.value.data.shape

            # update shape length in the axes widget
            self.axes_widget.update_axes_number(len(shape))
            self.axes_widget.set_text_field(self.axes_widget.get_default_text())

    def _add_image(self, image):
        if SAMPLE in self.viewer.layers:
            self.viewer.layers.remove(SAMPLE)

        if image is not None:
            self.viewer.add_image(image, name=SAMPLE, visible=True)
            self.sample_image = image

            # update the axes widget
            self.axes_widget.update_axes_number(len(image.shape))
            self.axes_widget.set_text_field(self.axes_widget.get_default_text())

    def _update_disk_axes(self):
        path = self.images_folder.get_folder()

        # load one image
        load_worker = loading_worker(path)
        load_worker.yielded.connect(lambda x: self._add_image(x))
        load_worker.start()

    def _update_tab_axes(self):
        """
        Updates the axes widget following the newly selected tab.

        :return:
        """
        self.is_from_disk = self.tabs.currentIndex() == 1

        if self.is_from_disk:
            self._update_disk_axes()
        else:
            self._update_layer_axes()

    def _update(self, updates):
        if UpdateType.N_IMAGES in updates:
            self.n_im = updates[UpdateType.N_IMAGES]
            self.pb_prediction.setValue(0)
            self.pb_prediction.setFormat(f'Prediction 0/{self.n_im}')

        if UpdateType.IMAGE in updates:
            val = updates[UpdateType.IMAGE]
            perc = int(100 * val / self.n_im + 0.5)
            self.pb_prediction.setValue(perc)
            self.pb_prediction.setFormat(f'Prediction {val}/{self.n_im}')
            self.viewer.layers[DENOISING].refresh()

        if UpdateType.DONE in updates:
            self.pb_prediction.setValue(100)
            self.pb_prediction.setFormat(f'Prediction done')

    def _start_prediction(self):
        if self.state == State.IDLE:
            if self.axes_widget.is_valid() and not Path(self.get_model_path()).is_dir():
                self.state = State.RUNNING

                self.predict_button.setText('Stop')

                if DENOISING in self.viewer.layers:
                    self.viewer.layers.remove(DENOISING)

                if self.is_from_disk == 0:
                    # from napari layers
                    im_shape = self.images.value.data.shape
                    current_axes = self.get_axes()
                    final_shape = get_napari_shapes(im_shape, current_axes)

                    self.denoi_prediction = np.zeros(final_shape, dtype=np.float32).squeeze()
                    self.viewer.add_image(self.denoi_prediction, name=DENOISING, visible=True)
                else:
                    self.denoi_prediction = np.zeros((1,), dtype=np.float32).squeeze()
                    self.viewer.add_image(self.denoi_prediction, name=DENOISING, visible=True)

                self.worker = prediction_worker(self)
                self.worker.yielded.connect(lambda x: self._update(x))
                self.worker.returned.connect(self._done)
                self.worker.start()
            else:
                # TODO feedback to users
                pass
        elif self.state == State.RUNNING:
            self.state = State.IDLE

    def _done(self):
        self.state = State.IDLE
        self.predict_button.setText('Predict again')

    def get_model_path(self):
        return self.load_model_button.Model.value

    # TODO call these methods throughout the workers
    def get_axes(self):
        return self.axes_widget.get_axes()


if __name__ == "__main__":
    from napari_n2v._sample_data import n2v_2D_data, n2v_3D_data

    # create a Viewer
    viewer = napari.Viewer()

    # add our plugin
    viewer.window.add_dock_widget(PredictWidget(viewer))

    # add images
    dim = '2D'
    if dim == '2D':
        data = n2v_2D_data()
        viewer.add_image(data[0][0][-10:], name=data[0][1]['name'])
    else:
        data = n2v_3D_data()
        viewer.add_image(data[0][0][4:20, 150:378, 150:378], name=data[0][1]['name'])
        #viewer.add_image(data[0][0], name=data[0][1]['name'])

    napari.run()
