import os
import scipy
import numpy as np
from distutils.version import StrictVersion
import skimage
import xlsxwriter
from skimage.filters import sobel
from skimage.morphology import watershed
from skimage.filters import gabor_kernel
from scipy.signal import convolve2d
from skimage import measure
from skimage.measure import label
from sklearn.linear_model import LogisticRegression
from qtpy import uic
from skimage.morphology import binary_dilation
import json, codecs


import flika
from flika.roi import makeROI
from flika import global_vars as g
from flika.process import difference_of_gaussians, threshold, zproject, remove_small_blobs
from flika.window import Window
from flika.process.file_ import open_file, close
from flika.utils.misc import save_file_gui, open_file_gui

flika_version = flika.__version__
if StrictVersion(flika_version) < StrictVersion('0.2.23'):
    from flika.process.BaseProcess import BaseProcess, WindowSelector, SliderLabel, CheckBox
else:
    from flika.utils.BaseProcess import BaseProcess, WindowSelector, SliderLabel, CheckBox


from .marking_binary_window import Classifier_Window
from . import mysql_interface

def show_label_img(binary_img):
    A = label(binary_img, connectivity=2)
    I = np.zeros((np.max(A), A.shape[0], A.shape[1]), dtype=np.bool)
    for i in np.arange(1, np.max(A)):
        I[i-1] = A == i
    return Window(I)

def get_important_features(binary_image):
    features = {}
    # important features include:
    # convexity: ratio of convex_image area to image area
    # area: number of pixels total
    # eccentricity: 0 is a circle, 1 is a line
    label_img = label(binary_image, connectivity=2)
    props = measure.regionprops(label_img)
    features['convexity'] = np.array([p.filled_area / p.convex_area for p in props])
    features['eccentricity'] = np.array([p.eccentricity for p in props])
    features['area'] = np.array([p.filled_area for p in props]) / 4000
    features['circularity'] = np.array([p.filled_area*(4*np.pi)/p.perimeter**2 for p in props])
    return features
    p = pg.plot(features['convexity'], pen=pg.mkPen('r'))
    p.plot(features['eccentricity'], pen=pg.mkPen('g'))
    p.plot(features['area'], pen=pg.mkPen('y'))

def remove_borders(binary_image):
    label_img = label(binary_image, connectivity=2)
    mx, my = binary_image.shape
    border_labels = set(label_img[:, 0]) | set(label_img[0, :]) | set(label_img[mx-1, :]) | set(label_img[:, my-1])
    for i in border_labels:
        binary_image[label_img==i] = 0
    return binary_image

def remove_false_positives(binary_window, features):
    label_img = binary_window.labeled_img
    nElements = np.max(label_img)
    for i in np.arange(nElements):
        if features['area'][i] < .05:
            binary_window.roi_states[i] = 2
        elif features['convexity'][i] < .7:
            binary_window.roi_states[i] = 2
        elif features['eccentricity'][i] > .96 and features['convexity'][i] < .85:
            binary_window.roi_states[i] = 2
        elif features['area'][i] > 3:
            binary_window.roi_states[i] = 2
        elif features['circularity'][i] < 0.4:
            binary_window.roi_states[i] = 2
        else:
            binary_window.roi_states[i] = 1
    binary_window.colored_img = np.repeat(binary_window.image[:, :, np.newaxis], 3, 2)
    binary_window.colored_img[binary_window.image == 1] = Classifier_Window.GREEN
    for roi_num, new_state in enumerate(binary_window.roi_states):
        if new_state != 1:
            color = [Classifier_Window.WHITE, Classifier_Window.GREEN, Classifier_Window.RED][new_state]
            binary_window.colored_img[binary_window.labeled_img == roi_num + 1] = color
    binary_window.update_image(binary_window.colored_img)

def generate_kernel(theta=0):
    frequency = .1
    sigma_x = 1  # left right axis. Bigger this number, smaller the width
    sigma_y = 2  # right left axis. Bigger this number, smaller the height
    kernel = np.real(gabor_kernel(frequency, theta, sigma_x, sigma_y))
    kernel -= np.mean(kernel)
    return kernel

def get_kernels():
    # prepare filter bank kernels
    kernels = []
    for theta in np.linspace(0, np.pi, 40):
        kernel = generate_kernel(theta)
        kernels.append(kernel)
    return kernels

kernels = get_kernels()

def convolve_with_kernels_fft(I, kernels):
    results = []
    for k, kernel in enumerate(kernels):
        print(k)
        filtered = scipy.signal.fftconvolve(I, kernel, 'same')
        results.append(filtered)
    results = np.array(results)
    return results

def plot_regression_results(X1, X2, y):
    p = pg.plot()
    x1 = X1[y==1]
    x2 = X2[y==1]
    s1 = pg.ScatterPlotItem(x1, x2, size=10, pen=None, brush=pg.mkBrush(0, 255, 0, 255))
    p.addItem(s1)
    x1 = X1[y==0]
    x2 = X2[y==0]
    s1.addPoints(x1, x2, size=10, pen=None, brush=pg.mkBrush(255, 0, 0, 255))

def get_border_between_two_props(prop1, prop2):
    I2 = np.copy(prop2.image)
    I1 = np.zeros_like(I2)
    bbox = np.array(prop2.bbox)
    top_left = bbox[:2]
    a = prop1.coords - top_left
    I1[a[:, 0], a[:, 1]] = 1
    # Window(I1.astype(np.int) + I2.astype(np.int))
    I1_expanded1 = binary_dilation(I1)
    I1_expanded2 = binary_dilation(binary_dilation(I1_expanded1))
    I1_expanded2[I1_expanded1] = 0
    border = I1_expanded2 * I2
    #Window(I1.astype(np.int) + I2.astype(np.int) + 2*border.astype(np.int))
    return np.argwhere(border) + top_left

def get_new_I(I, thresh1=.20, thresh2=.30):
    label_im_1 = label(I < thresh1, connectivity=2)
    label_im_2 = label(I < thresh2, connectivity=2)
    props_1 = measure.regionprops(label_im_1)
    props_2 = measure.regionprops(label_im_2)
    borders = np.zeros_like(I)
    #  The maximum of the labeled image is the number of contiguous regions, or ROIs.
    nROIs = np.max(label_im_1)
    for roi_num in np.arange(nROIs):
        # roi_num = 227 - 1
        prop1 = props_1[roi_num]
        x, y = prop1.coords[0,:]
        prop2 = props_2[label_im_2[x, y] - 1]
        if prop1.area > 200:
            perim_ratio = prop2.perimeter/prop1.perimeter
            if perim_ratio > 1.5:
                border_idx = get_border_between_two_props(prop1, prop2)
                borders[border_idx[:,0], border_idx[:, 1]] = 1

    I_new = np.copy(I)
    I_new[np.where(borders)] = 2
    #I_new = I + .2 * borders
    return I_new




class Myoquant():
    """myoquant()
    Muscle Cell Analysis Software
    """

    def __init__(self):
        pass

    def gui(self):
        self.roiStates = None
        self.classifier_window = None
        self.lines_win = None
        gui = uic.loadUi(os.path.join(os.path.dirname(__file__), 'myoquant.ui'))
        self.algorithm_gui = gui
        gui.show()
        self.original_window_selector = WindowSelector()
        self.original_window_selector.valueChanged.connect(self.create_markers_win)
        gui.gridLayout_18.addWidget(self.original_window_selector)
        self.threshold1_slider = SliderLabel(3)
        self.threshold1_slider.setRange(0, 1)
        self.threshold1_slider.setValue(.22)
        self.threshold1_slider.valueChanged.connect(self.threshold_slider_changed)
        self.threshold2_slider = SliderLabel(2)
        self.threshold2_slider.setRange(0, 1)
        self.threshold2_slider.setValue(.8)
        self.threshold2_slider.valueChanged.connect(self.threshold_slider_changed)
        gui.gridLayout_9.addWidget(self.threshold1_slider)
        gui.gridLayout_16.addWidget(self.threshold2_slider)
        gui.fill_boundaries_button.pressed.connect(self.fill_boundaries_button)
        gui.logistic_regression_button.pressed.connect(self.run_logistic_regression)
        gui.SVM_button.pressed.connect(self.run_SVM_classification_on_labeled_image)
        gui.SVM_saved_button.pressed.connect(self.run_SVM_classification_on_saved_training_data)

        self.validation_manual_selector = WindowSelector()
        self.validation_manual_selector.valueChanged.connect(self.validate)
        gui.gridLayout_11.addWidget(self.validation_manual_selector)
        self.validation_automatic_selector = WindowSelector()
        self.validation_automatic_selector.valueChanged.connect(self.validate)
        gui.gridLayout_12.addWidget(self.validation_automatic_selector)

        gui.save_fiber_button.pressed.connect(self.save_fiber_data)
        gui.mysql_export_button.pressed.connect(self.mysql_export_fiber_data)
        gui.load_binary_button.pressed.connect(self.load_binary_image)

        gui.closeEvent = self.closeEvent

    def validate(self):
        print('validating...')
        if self.validation_manual_selector.window is None:
            return None
        if self.validation_automatic_selector.window is None:
            return None
        man = self.validation_manual_selector.window
        auto = self.validation_automatic_selector.window
        man_states = man.roi_states
        auto_states = auto.roi_states
        assert len(man_states) == len(auto_states)
        true_positives = np.count_nonzero(np.logical_and(man_states == 1, auto_states == 1))
        true_negatives = np.count_nonzero(np.logical_and(man_states == 2, auto_states == 2))
        false_positives = np.count_nonzero(np.logical_and(man_states == 2, auto_states == 1))
        false_negatives = np.count_nonzero(np.logical_and(man_states == 1, auto_states == 2))
        precision = true_positives / (true_positives + false_positives)
        recall = true_positives / (true_positives + false_negatives)
        f1_score = 2 * (precision * recall) / (precision + recall)
        self.algorithm_gui.true_pos_label.setText(str(true_positives))
        self.algorithm_gui.true_neg_label.setText(str(true_negatives))
        self.algorithm_gui.false_pos_label.setText(str(false_positives))
        self.algorithm_gui.false_neg_label.setText(str(false_negatives))
        self.algorithm_gui.precision_label.setText(str(precision))
        self.algorithm_gui.recall_label.setText(str(recall))
        self.algorithm_gui.f1_score_label.setText(str(f1_score))

    def load_binary_image_cmd(self, fname):
        win = open_file(fname)
        self.add_classifier_window(win.image)
        win.close()

    def load_binary_image(self):
        win = open_file(from_gui=True)
        self.add_classifier_window(win.image)
        win.close()

    def create_markers_win(self):
        if self.original_window_selector.window is None:
            g.alert('You must select a Window before creating the markers window.')
        else:
            win = self.original_window_selector.window
            if np.max(win.image) > 1:
                g.alert("The window you select must have values between 0 and 1. Scaling the window now.")
                I = win.image.astype(np.float)
                I -= np.min(I)
                I /= np.max(I)
                win.image = I
                win.dtype = I.dtype
                win.imageview.setImage(win.image)
                win._init_dimensions(win.image)
                win.imageview.ui.graphicsView.addItem(win.top_left_label)
            original = win.image
            self.markers_win = Window(np.zeros_like(original, dtype=np.uint8), 'Binary Markers')
            self.markers_win.imageview.setLevels(-.1, 2.1)
            self.threshold1_slider.setRange(np.min(original), np.max(original))
            self.threshold2_slider.setRange(np.min(original), np.max(original))
            self.threshold_slider_changed()

            fname = self.original_window_selector.value().filename
            self.algorithm_gui.mousename.setText(os.path.splitext(os.path.basename(fname))[0])

    def threshold_slider_changed(self):
        if self.original_window_selector.window is None:
            g.alert('You must select a Window before adjusting the levels.')
        else:
            thresh1 = self.threshold1_slider.value()
            thresh2 = self.threshold2_slider.value()
            I = self.original_window_selector.window.image
            markers = (I > thresh1).astype(dtype=np.uint8)
            markers[I > thresh2] = 2
            self.markers_win.imageview.setImage(markers, autoRange=False, autoLevels=False)

    def add_classifier_window(self, I):
        if self.classifier_window is not None:
            self.algorithm_gui.gridLayout_17.removeWidget(self.classifier_window)
            self.classifier_window.setParent(None)
            self.classifier_window.close()
        self.classifier_window = Classifier_Window(I)
        self.algorithm_gui.gridLayout_17.addWidget(self.classifier_window)
        self.algorithm_gui.analyze_tab_widget.setCurrentIndex(1)

    def fill_boundaries_button(self):
        lower_bound = self.threshold1_slider.value()
        upper_bound = self.threshold2_slider.value()
        thresholds = np.linspace(lower_bound, upper_bound, 8)
        I = self.original_window_selector.window.image
        I_new = I
        for i in np.arange(len(thresholds) - 1):
            print(thresholds[i])
            I_new = get_new_I(I_new, thresholds[i], thresholds[i + 1])
        self.filled_boundaries_win = Window(I_new, 'Filled Boundaries')
        classifier_image = remove_borders(I_new < upper_bound)
        self.add_classifier_window(classifier_image)

    def get_norm_coeffs(self, X):
        mean = np.mean(X, 0)
        std = np.std(X, 0)
        return mean, std

    def normalize_data(self, X, mean, std):
        X = X - mean
        X = X / (2 * std)
        return X

    def run_SVM_classification_on_labeled_image(self):
        X_train, y_train = self.classifier_window.get_training_data()
        mu, sigma = self.get_norm_coeffs(self.classifier_window.features_array)
        self.run_SVM_classification_general(X_train, y_train, mu, sigma)

    def run_SVM_classification_on_saved_training_data(self):
        filename = open_file_gui("Open training_data", filetypes='*.json')
        if filename is None:
            return None
        obj_text = codecs.open(filename, 'r', encoding='utf-8').read()
        data = json.loads(obj_text)
        X_train = np.array(data['features'])
        y_train = np.array(data['states'])
        mu, sigma = self.get_norm_coeffs(X_train)
        self.run_SVM_classification_general(X_train, y_train, mu, sigma)

    def run_SVM_classification_general(self, X_train, y_train, mu, sigma):
        print('Running SVM classification')
        from sklearn import svm
        X_train = self.normalize_data(X_train, mu, sigma)
        clf = svm.SVC()
        clf.fit(X_train, y_train)
        X_test = self.normalize_data(self.classifier_window.get_features_array(), mu, sigma)
        y = clf.predict(X_test)
        roi_states = np.zeros_like(y)
        roi_states[y == 1] = 1
        roi_states[y == 0] = 2
        result_win = Classifier_Window(self.classifier_window.image)
        X = self.classifier_window.features_array

        ######################################################################################
        ##############   Add hand-designed rules here if you want  ###########################
        ######################################################################################
        # For instance, you could remove all ROIs smaller than 15 pixels like this:

        #roi_states[X[:, 0] < 15] = 2 # Area must be smaller than 15 pixels
        #roi_states[X[:, 3] < 0.6] = 2 # Convexity must be smaller than 0.6

        self.roiStates = roi_states
        result_win.set_roi_states(roi_states)
        self.algorithm_gui.model_params_label.setText('')

    def run_logistic_regression(self):
        X, y = self.classifier_window.get_training_data()
        self.logreg = LogisticRegression(C=1e9)
        self.logreg.fit(X, y)
        print('Accuracy = {}'.format(self.logreg.score(X,y)))
        X = self.classifier_window.features_array
        y = self.logreg.predict(X)
        result_win = Classifier_Window(self.classifier_window.image)
        roi_states = np.zeros_like(y)
        roi_states[y == 1] = 1
        roi_states[y == 0] = 2


        ######################################################################################
        ##############   Add hand-designed rules here if you want  ###########################
        ######################################################################################
        # For instance, you could remove all ROIs smaller than 20 pixels like this:
        roi_states[X[:, 0] < 15] = 2
        roi_states[X[:, 3] < 0.6] = 2




        self.roiStates = roi_states
        result_win.set_roi_states(roi_states)
        params = list(self.logreg.intercept_) + list(self.logreg.coef_[0])
        params = ', '.join(['Beta_' + str(i) + '=' + str(coef) for i, coef in enumerate(params) ])
        self.algorithm_gui.model_params_label.setText(params)

    def save_fiber_data(self):
        scaleFactor = self.algorithm_gui.microns_per_pixel_SpinBox.value()
        if not isinstance(g.win, Classifier_Window):
            g.alert('Make sure the window containing the data you are trying to export is selected (highlighted in green).')
            return
        X = g.win.get_extended_features_array()
        X = X[g.win.roi_states == 1]
        X[:, 0] /= scaleFactor**2  # area
        X[:, 4] *= scaleFactor  # minor axis
        fileSaveAsName = save_file_gui('Save file as...', filetypes='.xlsx')
        workbook = xlsxwriter.Workbook(fileSaveAsName)
        worksheet = workbook.add_worksheet()
        header = ['Area', 'Eccentricity', 'Convexity', 'Circularity', 'ROI #', 'Minor axis length']
        worksheet.write_row(0, 0, header)
        for row_idx, row_data in enumerate(X):
            worksheet.write_row(row_idx + 1, 0, row_data)
        workbook.close()

    def mysql_export_fiber_data(self):
        scaleFactor = self.algorithm_gui.microns_per_pixel_SpinBox.value()
        if not isinstance(g.win, Classifier_Window):
            g.alert('Make sure the window containing the data you are trying to export is selected (highlighted in green).')
            return
        X = g.win.get_extended_features_array()
        X = X[g.win.roi_states == 1]
        X[:, 0] /= scaleFactor**2  # area
        X[:, 4] *= scaleFactor  # minor axis
        # ['Area', 'Eccentricity', 'Convexity', 'Circularity', 'ROI #', 'Minor axis length']
        mousename = self.algorithm_gui.mousename.text()
        msg = mysql_interface.add_fibers(mousename, X)
        g.alert(msg)

    def closeEvent(self, event):
        print('Closing myoquant gui')
        if self.classifier_window is not None:
            self.classifier_window.close()
        event.accept() # let the window close

myoquant = Myoquant()
g.myoquant = myoquant


def testing():
    from plugins.myoquant.marking_binary_window import Classifier_Window
    original = open_file(r'C:\Users\kyle\Desktop\tmp.tif')
    binary_tmp = open_file(r'C:\Users\kyle\Desktop\binary.tif')
    binary = Classifier_Window(binary_tmp.image, 'Classifier Window')
    close(binary_tmp)
    binary.load_classifications(r'C:\Users\kyle\Desktop\classifications.json')


if __name__ == '__main__':
    original = open_file(r'C:\Users\kyle\Dropbox\Software\2017 Jennas cell counting\mdx_224_Laminin.tif')
    split_channels()
    crop
    original = resize(2)
    g.myoquant.gui()

