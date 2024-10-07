import sys
import os
import multiprocessing
from PyQt5.QtWidgets import (
    QApplication,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QFileDialog,
    QLabel,
    QSlider,
    QHBoxLayout,
    QFormLayout,
)
from PyQt5.QtCore import Qt
import open3d as o3d
import numpy as np
import laspy
from basal_points_algo import (
    BasalPointSelection,
    BasalPointAlgorithm,
)
from scipy.spatial import cKDTree
from concurrent.futures import ThreadPoolExecutor, as_completed
import tempfile
import os

def show_point_cloud(points_or_mesh_path, colors=None, is_mesh=False):
    """
    Visualize the point cloud or mesh using Open3D.
    """
    if not is_mesh:
        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(points_or_mesh_path)
        if colors is not None:
            pcd.colors = o3d.utility.Vector3dVector(colors)

        # Estimate normals for the point cloud to enhance visualization
        pcd.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.05, max_nn=50)
        )
        geometry = pcd
    else:
        # Load the mesh from the file
        geometry = o3d.io.read_triangle_mesh(points_or_mesh_path)
        
    o3d.visualization.draw_geometries([geometry])

# Point picking function
def pick_points(self, pcd):
    """
    Pick points from the point cloud for seed selection.
    """

    # Terminate any existing visualizations before starting point picking
    if self.process:
        self.process.terminate()

    # Initialize Open3D visualizer with editing capability for point picking
    self.seed_selection_vis = o3d.visualization.VisualizerWithEditing()
    self.seed_selection_vis.create_window()

    # Set uniform color for the point cloud for better visibility during selection
    pcd.paint_uniform_color([0.5, 0.5, 0.5])

    # Estimate normals for the point cloud to assist in point picking
    pcd.estimate_normals(
        search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.05, max_nn=50)
    )

    # Add the point cloud to the visualizer
    self.seed_selection_vis.add_geometry(pcd)
    self.seed_selection_vis.run()  # user picks points

    # Retrieve the indices of the picked points
    picked_points = self.seed_selection_vis.get_picked_points()
    self.seed_selection_vis.destroy_window()  # Ensure the window is closed
    self.seed_selection_vis = None
    return picked_points


# Load LAS file and convert to Open3D point cloud
def load_las_as_open3d_point_cloud(self, las_file_path, evaluate=False):
    """
    Load a LAS file and convert it to an Open3D point cloud.
    """

    # Read LAS file using laspy
    pc = laspy.read(las_file_path)
    x, y, z = pc.x, pc.y, pc.z
    ground_truth_labels = None

    # Check if ground truth labels are available for evaluation
    if evaluate and "Original cloud index" in pc.point_format.dimension_names:
        ground_truth_labels = np.int_(pc["Original cloud index"])

    # Store the mean values for recentering later
    self.x_mean = np.mean(x)
    self.y_mean = np.mean(y)
    self.z_mean = np.mean(z)

    # Recenter the point cloud
    xyz = np.vstack((x - self.x_mean, y - self.y_mean, z - self.z_mean)).transpose()

    # Check if RGB color information is available in the LAS file
    if all(dim in pc.point_format.dimension_names for dim in ["red", "green", "blue"]):
        r = np.uint8(pc.red / 65535.0 * 255)
        g = np.uint8(pc.green / 65535.0 * 255)
        b = np.uint8(pc.blue / 65535.0 * 255)
        rgb = np.vstack((r, g, b)).transpose() / 255.0
    else:
        rgb = np.zeros((len(x), 3))

    # Create Open3D PointCloud object and set points and colors
    pcd = o3d.geometry.PointCloud()
    pcd.points = o3d.utility.Vector3dVector(xyz)
    pcd.colors = o3d.utility.Vector3dVector(rgb)

    self.pcd_colors = pcd.colors

    return pcd, ground_truth_labels


# Region growing segmentation
def region_growing(self, pcd, rock_seeds, pedestal_seeds):
    """
    Run the region growing segmentation on the point cloud using the selected seeds.
    """
    from RegionGrowing import RegionGrowingSegmentation

    # Convert seed indices to integers
    rock_seed_indices = list(map(int, rock_seeds))
    pedestal_seed_indices = list(map(int, pedestal_seeds))

    # Initialize the region growing segmentation with the selected seeds and basal points
    self.segmenter = RegionGrowingSegmentation(
        pcd,
        downsample=False,
        smoothness_threshold=self.smoothness_threshold,
        distance_threshold=0.05,
        curvature_threshold=self.curvature_threshold,
        rock_seeds=rock_seed_indices,
        pedestal_seeds=pedestal_seed_indices,
        basal_points=self.basal_points,
        basal_proximity_threshold=self.basal_proximity_threshold 
    )

    # Segment the point cloud and perform conditional label propagation
    segmented_pcd, labels = self.segmenter.segment()
    #if not np.any(self.basal_points):
    self.segmenter.conditional_label_propagation()

    # Assign a default label for unlabeled points
    labels[labels == -1] = 1

    # Color the segmented point cloud
    colored_pcd = self.segmenter.color_point_cloud()

    # Highlight proximity points if basal points are available
    # if np.any(self.basal_points):
    #     colored_pcd = self.segmenter.highlight_proximity_points(colored_pcd)

    return colored_pcd


# Main window class for the GUI
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Region Growing with PyQt and Open3D")
        self.pcd = None
        self.process = None
        self.rock_seeds = None
        self.pedestal_seeds = None
        self.poc_points = []
        self.basal_point_selection = None
        self.basal_points = None

        self.smoothness_threshold = 0.99  # Initial default value for the smoothness threshold (0-1)
        self.curvature_threshold = 0.15   # Initial default value for the curvature threshold (0-1)
        self.basal_proximity_threshold = 0.05  # Default value for basal proximity threshold (0-1)

        self.init_ui()

    def init_ui(self):
        """
        Initialize the UI components.
        """
        layout = QVBoxLayout()

        # Button to load a LAS file
        self.load_button = QPushButton("Load LAS File")
        self.load_button.clicked.connect(self.load_las_file)
        layout.addWidget(self.load_button)

        # Button to continue to seed selection
        self.continue_button = QPushButton("Continue to Select Seeds")
        self.continue_button.setEnabled(False)
        self.continue_button.clicked.connect(self.continue_to_select_seeds)
        layout.addWidget(self.continue_button)

        # Button to manually select seeds
        self.manual_selection_button = QPushButton("Select Seeds Manually")
        self.manual_selection_button.setVisible(False)
        self.manual_selection_button.clicked.connect(self.start_manual_selection)
        layout.addWidget(self.manual_selection_button)

        # Button to continue with the selected seeds
        self.continue_with_seeds_button = QPushButton("Continue with Selected Seeds")
        self.continue_with_seeds_button.setVisible(False)
        self.continue_with_seeds_button.clicked.connect(lambda: self.show_sliders(smoothness_threshold=0.99, curvature_threshold=0.15))
        layout.addWidget(self.continue_with_seeds_button)

        # Label to show instructions
        self.instructions_label = QLabel("")
        layout.addWidget(self.instructions_label)

        # Button to proceed to the next step
        self.next_button = QPushButton("Next")
        self.next_button.setVisible(False)
        self.next_button.clicked.connect(self.select_pedestal_seeds)
        layout.addWidget(self.next_button)

        self.slider_layout = QFormLayout()

        # Descriptions for sliders
        smoothness_description = QLabel("Controls surface smoothness variation; higher values include only smoother points.\n")
        curvature_description = QLabel("Sets the curvature limit; higher values allow more curved points.\n")
        #proximity_description = QLabel("Controls how close points should be to basal points to stop region growing.\n")


        # Smoothness Threshold Slider
        smoothness_slider_layout = QHBoxLayout()
        self.smoothness_slider = QSlider(Qt.Horizontal)
        self.smoothness_slider.setRange(0, 100)  # Slider range 0-100, but mapped to 0-1
        self.smoothness_slider.setValue(int(self.smoothness_threshold * 100))
        self.smoothness_slider.setMinimumWidth(300)
        self.smoothness_slider.valueChanged.connect(self.update_smoothness_threshold)
        smoothness_value_label = QLabel(f"{self.smoothness_threshold:.2f}")
        self.smoothness_slider.valueChanged.connect(lambda: smoothness_value_label.setText(f"{self.smoothness_threshold:.2f}"))
        smoothness_slider_layout.addWidget(self.smoothness_slider)
        smoothness_slider_layout.addWidget(smoothness_value_label)
        self.slider_layout.addRow("Smoothness Threshold", smoothness_slider_layout)
        self.slider_layout.addRow(smoothness_description)

        # Curvature Threshold Slider
        curvature_slider_layout = QHBoxLayout()
        self.curvature_slider = QSlider(Qt.Horizontal)
        self.curvature_slider.setRange(0, 100)  # Slider range 0-100, but mapped to 0-1
        self.curvature_slider.setValue(int(self.curvature_threshold * 100))
        self.curvature_slider.setMinimumWidth(300)
        self.curvature_slider.valueChanged.connect(self.update_curvature_threshold)
        curvature_value_label = QLabel(f"{self.curvature_threshold:.2f}")
        self.curvature_slider.valueChanged.connect(lambda: curvature_value_label.setText(f"{self.curvature_threshold:.2f}"))
        curvature_slider_layout.addWidget(self.curvature_slider)
        curvature_slider_layout.addWidget(curvature_value_label)
        self.slider_layout.addRow("Curvature Threshold", curvature_slider_layout)
        self.slider_layout.addRow(curvature_description)

       
        # Store the proximity slider label, slider, and proximity value label references
        self.proximity_slider_label = QLabel("Basal Proximity Threshold")

        proximity_slider_layout = QHBoxLayout()
        self.proximity_slider = QSlider(Qt.Horizontal)
        self.proximity_slider.setRange(0, 100)  # Slider range 0-100, but mapped to 0-1
        self.proximity_slider.setValue(int(self.basal_proximity_threshold * 100))
        self.proximity_slider.setMinimumWidth(300)
        self.proximity_slider.valueChanged.connect(self.update_basal_proximity_threshold)

        # Store reference to the value label that shows the slider's current value
        self.proximity_value_label = QLabel(f"{self.basal_proximity_threshold:.2f}")
        self.proximity_slider.valueChanged.connect(lambda: self.proximity_value_label.setText(f"{self.basal_proximity_threshold:.2f}"))

        # Add the slider and value label to the layout
        proximity_slider_layout.addWidget(self.proximity_slider)
        proximity_slider_layout.addWidget(self.proximity_value_label)

        # Add the proximity slider label and layout to the form layout
        self.slider_layout.addRow(self.proximity_slider_label, proximity_slider_layout)




        slider_widget = QWidget()
        slider_widget.setLayout(self.slider_layout)
        slider_widget.setVisible(False)
        self.slider_widget = slider_widget

        layout.addWidget(slider_widget)

        # Button to run the region growing algorithm
        self.run_button = QPushButton("Run Region Growing")
        self.run_button.setVisible(False)
        self.run_button.clicked.connect(self.run_region_growing)
        layout.addWidget(self.run_button)

        # Button to input points of contact
        self.input_poc_button = QPushButton("Input Points of Contact")
        self.input_poc_button.setVisible(False)
        self.input_poc_button.clicked.connect(self.input_point_of_contacts)
        layout.addWidget(self.input_poc_button)

        # Button to estimate basal points
        self.estimate_basal_points_button = QPushButton("Estimate Basal Points")
        self.estimate_basal_points_button.setVisible(False)
        self.estimate_basal_points_button.clicked.connect(self.estimate_basal_points)
        layout.addWidget(self.estimate_basal_points_button)

        # Button to reselect basal points
        self.add_more_basal_points_button = QPushButton("Reselect Basal Points")
        self.add_more_basal_points_button.setVisible(False)
        self.add_more_basal_points_button.clicked.connect(self.add_more_basal_points)
        layout.addWidget(self.add_more_basal_points_button)

        # Button to save the segmented point cloud
        self.save_pcd_button = QPushButton("Save Point Cloud")
        self.save_pcd_button.setVisible(False)
        self.save_pcd_button.clicked.connect(self.save_point_cloud)
        layout.addWidget(self.save_pcd_button)

        # Button to reconstruct mesh
        self.reconstruct_mesh_button = QPushButton("Reconstruct Mesh")
        self.reconstruct_mesh_button.setVisible(False)
        self.reconstruct_mesh_button.clicked.connect(self.reconstruct_mesh)
        layout.addWidget(self.reconstruct_mesh_button)

        # Button to save mesh
        self.save_mesh_button = QPushButton("Save Mesh")
        self.save_mesh_button.setVisible(False)
        self.save_mesh_button.clicked.connect(self.save_mesh)
        layout.addWidget(self.save_mesh_button)

        # Set the main layout and central widget for the window
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def update_smoothness_threshold(self, value):
        print("update_smoothness_threshold", value)
        self.smoothness_threshold = value / 100.0  # Scale slider value to 0-1

    def update_curvature_threshold(self, value):
        self.curvature_threshold = value / 100.0

    def update_basal_proximity_threshold(self, value):
        self.basal_proximity_threshold = value / 100.0  

    def show_sliders(self, smoothness_threshold=0.99, curvature_threshold=0.15):
        self.hide_all_buttons()
        self.slider_widget.setVisible(True)
        print("show_sliders", smoothness_threshold, curvature_threshold)

        # Update the smoothness and curvature sliders
        self.smoothness_slider.setValue(int(smoothness_threshold * 100))
        self.curvature_slider.setValue(int(curvature_threshold * 100))

        # Only show the proximity slider layout if basal points are available
        if self.basal_points is not None and len(self.basal_points) > 0:
            self.proximity_slider.setValue(int(self.basal_proximity_threshold * 100))
            self.proximity_slider_label.setVisible(True)  # Show label for proximity slider
            self.proximity_slider.setVisible(True)  # Show the proximity slider itself
            self.proximity_value_label.setVisible(True)  # Show the proximity value label
        else:
            self.proximity_slider_label.setVisible(False)  # Hide label for proximity slider
            self.proximity_slider.setVisible(False)  # Hide the proximity slider itself
            self.proximity_value_label.setVisible(False)  # Hide the proximity value label

        self.run_button.setVisible(True)



        
    def load_las_file(self):
        """
        Load a LAS file and display the point cloud.
        """
        options = QFileDialog.Options()
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "Open LAS File",
            "",
            "LAS Files (*.las);;All Files (*)",
            options=options,
        )
        if file_name:
            # Load the LAS file as an Open3D point cloud
            self.pcd, _ = load_las_as_open3d_point_cloud(self, file_name)
            points = np.asarray(self.pcd.points)
            colors = np.asarray(self.pcd.colors)

            # Close any existing visualization windows
            if self.process:
                self.process.terminate()

            # Start a new process to display the point cloud
            self.process = multiprocessing.Process(
                target=show_point_cloud, args=(points, colors)
            )
            self.process.start()
            # Enable the continue button to proceed to seed selection
            self.continue_button.setEnabled(True)

    def continue_to_select_seeds(self):
        """
        Continue to seed selection after loading the point cloud.
        """
        self.continue_button.setEnabled(False)
        voxel_size = 0.01
        self.pcd = self.pcd.voxel_down_sample(voxel_size)

        # Computer selected seeds for rock and pedestal
        points = np.asarray(self.pcd.points)
        min_bound = points.min(axis=0)
        max_bound = points.max(axis=0)
        centroid_x = (min_bound[0] + max_bound[0]) / 2.0
        centroid_y = (min_bound[1] + max_bound[1]) / 2.0

        distances = np.linalg.norm(
            points[:, :2] - np.array([centroid_x, centroid_y]), axis=1
        )
        highest_point_index = np.argmax(points[:, 2] - distances)  # seed for rock
        bottommost_point_index = np.argmin(points[:, 2])  # seed for pedestal

        self.rock_seeds = [highest_point_index]
        self.pedestal_seeds = [bottommost_point_index]

        colors = np.full(points.shape, [0.5, 0.5, 0.5])

        # Highlight seeds in different colors
        colors[highest_point_index] = [1, 0, 0]  # Red for rock
        colors[bottommost_point_index] = [0, 0, 1]  # Blue for pedestal

        self.pcd.colors = o3d.utility.Vector3dVector(colors)
        points = np.asarray(self.pcd.points)
        colors = np.asarray(self.pcd.colors)

        if self.process:
            self.process.terminate()

        self.process = multiprocessing.Process(
            target=show_point_cloud, args=(points, colors)
        )
        self.process.start()

        # Make buttons for manual seed selection and continuing with selected seeds visible
        self.manual_selection_button.setVisible(True)
        self.continue_with_seeds_button.setVisible(True)
        self.continue_button.setVisible(False)

    def get_selected_points_close_window(self):
        """
        Retrieve selected points and close the visualization window.
        """
        if self.seed_selection_vis is not None:
            selected_points = self.seed_selection_vis.get_picked_points()
            self.seed_selection_vis.destroy_window()  # Ensure the window is closed
            self.seed_selection_vis = None

        return selected_points

    def start_manual_selection(self):
        """
        Start manual selection of seeds.
        """
        # Hide all buttons except the 'Next' button to guide the user through the process
        self.hide_buttons_except_next()

        # Update the instructions label with steps for selecting rock seeds
        self.instructions_label.setText(
            "Currently selecting seeds for Rock.\n \n"
            "1) Please pick points using [shift + left click].\n"
            "2) Press [shift + right click] to undo point picking.\n"
            "3) After picking points, press 'Next' to select Pedestal seeds."
        )
        self.next_button.setVisible(True)

        # Terminate any existing processes before starting point picking
        self.seed_selection_vis = None
        if self.process:
            self.process.terminate()

        # Call the function to allow the user to pick points
        self.rock_seeds = pick_points(self, self.pcd) 

    def select_pedestal_seeds(self):
        """
        Select pedestal seeds after rock seeds have been selected.
        """
        self.show_sliders()
        self.rock_seeds = self.get_selected_points_close_window()
        self.instructions_label.setText(
            "Currently selecting seeds for Pedestal.\n \n"
            "1) Please pick points using [shift + left click].\n"
            "2) Press [shift + right click] to undo point picking.\n"
            "3) After picking points, press the below button to Run Region Growing."
        )
        self.run_button.setVisible(True)
        if self.process:
            self.process.terminate()

        pick_points(self, self.pcd)

    def run_region_growing(self):
        """
        Run the region growing algorithm with the selected seeds or with basal information.
        """

        if self.process:
            self.process.terminate()

        # Hide all buttons except the 'Next' button to guide the user through the process
        self.hide_all_buttons()

        # Check if basal points are available or not
        if not np.any(self.basal_points):
            # If basal points are not available, it means region growing is being run for the first time after manual seed selection,
            # so get and store the selected seeds
            if self.pedestal_seeds is None:
                self.pedestal_seeds = self.get_selected_points_close_window()

        self.instructions_label.setText("Running Region Growing. Please wait...")
        QApplication.processEvents()

        # Run the region growing algorithm with the selected seeds
        colored_pcd = region_growing(
            self, self.pcd, self.rock_seeds, self.pedestal_seeds
        )
        self.instructions_label.setText("")

        # If no basal points are present, it means we are running region growing for the first time after manual seed selection,
        # so show the input point of contact button
        if not np.any(self.basal_points):
            self.input_poc_button.setVisible(True)

        # Make the save point cloud button visible
        self.save_pcd_button.setVisible(True)
        self.reconstruct_mesh_button.setVisible(True)

        # Start a new process to show the segmented point cloud
        self.process = multiprocessing.Process(
            target=show_point_cloud,
            args=(np.asarray(colored_pcd.points), np.asarray(colored_pcd.colors)),
        )
        self.process.start()


    def input_point_of_contacts(self):
        """
        Input points of contact for the region growing algorithm.
        """
        # Close all the visualization windows before starting point picking
        if self.process:
            self.process.terminate()

        self.hide_buttons_for_poc()
        self.instructions_label.setText(
            "Selecting Points of Contact.\n \n"
            "1) Please pick points using [shift + left click].\n"
            "2) Press [shift + right click] to undo point picking.\n"
            "3) After picking points, press 'Next' to estimate basal points."
        )
        self.estimate_basal_points_button.setVisible(True)

        if self.process:
            self.process.terminate()

        # Call the function to allow the user to pick points
        self.basal_points = None
        pick_points(self, self.pcd)

    def estimate_basal_points(self):
        """
        Estimate basal points based on the selected points of contact.
        """
        self.poc_points = self.get_selected_points_close_window()
        self.instructions_label.setText("Estimating basal points. Please wait...")
        QApplication.processEvents()

        # Use BasalPointAlgorithm to estimate the basal points
        algorithm = BasalPointAlgorithm(self.pcd)
        basal_points = algorithm.run(self.poc_points)

        # Add the newly estimated basal points to the list
        self.basal_points = basal_points

        # Highlight the basal points in the point cloud
        self.highlight_points(self.basal_points)

        self.show_sliders(smoothness_threshold=0.9, curvature_threshold=0.1)  

        self.instructions_label.setText(
            "Basal points estimation completed. Would you like to add more basal points or run region growing again?"
        )
        self.estimate_basal_points_button.setVisible(False)
        self.add_more_basal_points_button.setVisible(True)
        self.run_button.setVisible(True)
        # self.save_pcd_button.setVisible(True)
        show_point_cloud(np.asarray(self.pcd.points), np.asarray(self.pcd.colors))

    def add_more_basal_points(self):
        """
        Allow the user to reselect or add more basal points.
        """
        # Hide the buttons related to basal points and region growing
        self.add_more_basal_points_button.setVisible(False)
        self.run_button.setVisible(False)

        # Allow the user to reselect points of contact
        self.input_point_of_contacts()

    def highlight_points(self, points):
        """
        Highlight estimated basal points in the point cloud.
        """
        colors = np.full((len(self.pcd.points), 3), [0.5, 0.5, 0.5])
        for point in points:
            idx = np.argmin(np.linalg.norm(np.asarray(self.pcd.points) - point, axis=1))
            colors[idx] = [1, 0, 0]  # Red for basal points

        self.pcd.colors = o3d.utility.Vector3dVector(colors)
        points = np.asarray(self.pcd.points)
        colors = np.asarray(self.pcd.colors)

        if self.process:
            self.process.terminate()

        self.process = multiprocessing.Process(
            target=show_point_cloud, args=(points, colors)
        )
        self.process.start()

    def save_point_cloud(self):
        """
        Save rock region points, pedestal points, and basal points to a LAS file.
        Intensity values: Rock = 0, Pedestal = 1, Basal = 2
        """
        options = QFileDialog.Options()
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Save Point Cloud",
            "",
            "LAS Files (*.las);;All Files (*)",
            options=options,
        )
        if file_name:
            if not file_name.lower().endswith(".las"):
                file_name += ".las"

            points = np.asarray(self.pcd.points)
            labels = np.asarray(self.segmenter.labels)

            # Create intensity array based on labels
            intensity = np.zeros(len(points), dtype=np.uint16)
            intensity[labels == 0] = 0  # Pedestal points
            intensity[labels == 1] = 1  # Rock points

            # Add basal points if they exist
            if self.basal_points is not None:
                basal_points = np.asarray(self.basal_points)
                points = np.vstack((points, basal_points))
                basal_intensity = np.full(len(basal_points), 2, dtype=np.uint16)
                intensity = np.hstack((intensity, basal_intensity))

            # Undo the recentering
            points[:, 0] += self.x_mean
            points[:, 1] += self.y_mean
            points[:, 2] += self.z_mean

            # Create a new LAS file
            header = laspy.LasHeader(point_format=3, version="1.2")
            las = laspy.LasData(header)

            # Assign coordinates and intensity
            las.x = points[:, 0]
            las.y = points[:, 1]
            las.z = points[:, 2]
            las.intensity = intensity

            # Assign colors
            colors = (np.asarray(self.pcd.colors) * 65535).astype(np.uint16)
            if self.basal_points is not None:
                basal_colors = np.full((len(basal_points), 3), [65535, 0, 0], dtype=np.uint16)  # Red for basal points
                colors = np.vstack((colors, basal_colors))

            las.red = colors[:, 0]
            las.green = colors[:, 1]
            las.blue = colors[:, 2]

            # Write the LAS file
            las.write(file_name)

    @staticmethod
    def detect_basal_points_optimized(points, labels, k=30, threshold=0.35):
        tree = cKDTree(points)
        distances, indices = tree.query(points, k=k)
        
        neighborhood_labels = labels[indices]
        rock_ratios = np.sum(neighborhood_labels == 1, axis=1) / k
        
        basal_points = (threshold <= rock_ratios) & (rock_ratios <= (1 - threshold))
        return basal_points

    def reconstruct_mesh(self):
        self.hide_all_buttons()
        self.instructions_label.setText("Reconstructing mesh. Please wait...")
        QApplication.processEvents()

        # Perform basal detection if not available
        if self.basal_points is None:
            points = np.asarray(self.pcd.points)
            labels = np.asarray(self.segmenter.labels)
            self.basal_points = self.detect_basal_points_optimized(points, labels)
            print(f"Detected {np.sum(self.basal_points)} basal points.")

        # Filter the point cloud to keep only rock and basal points
        rock_points = np.asarray(self.segmenter.labels) == 1
        filtered_indices = np.logical_or(rock_points, self.basal_points)
        filtered_points = np.asarray(self.pcd.points)[filtered_indices]
        filtered_colors = np.asarray(self.pcd.colors)[filtered_indices]

        # Create a new point cloud with only rock and basal points
        filtered_pcd = o3d.geometry.PointCloud()
        filtered_pcd.points = o3d.utility.Vector3dVector(filtered_points)
        filtered_pcd.colors = o3d.utility.Vector3dVector(filtered_colors)

        # Perform open face interpolation
        basal_indices = np.where(self.basal_points[filtered_indices])[0]
        interpolated_pcd = self.generate_boundary_filling_points(filtered_pcd, basal_indices, n_points=10)

        # Change the color of all the points in interpolated_pcd to red
        interpolated_pcd.paint_uniform_color([1, 0, 0])

        # Perform Poisson reconstruction
        mesh = self.poisson_reconstruction(interpolated_pcd)
        self.reconstructed_mesh = mesh
        

        self.instructions_label.setText("Mesh reconstruction completed.")
        self.save_mesh_button.setVisible(True)
        QApplication.processEvents()

        # Visualize the mesh
        if self.process:
            self.process.terminate()
        
        # Save the mesh to a temporary file
        with tempfile.NamedTemporaryFile(suffix='.ply', delete=False) as temp_file:
            temp_mesh_path = temp_file.name
            self.temp_mesh_path = temp_mesh_path
        o3d.io.write_triangle_mesh(temp_mesh_path, mesh)

        self.process = multiprocessing.Process(
            target=show_point_cloud, args=(temp_mesh_path, None, True)
        )
        self.process.start()

    @staticmethod
    def generate_boundary_filling_points(pcd, basal_indices, n_points=10):
        points = np.asarray(pcd.points)
        colors = np.asarray(pcd.colors)
        basal_points = points[basal_indices]

        # Calculate the centroid of the basal points
        centroid = np.mean(basal_points, axis=0)

        # Build KD-Tree for efficient nearest neighbor search
        tree = cKDTree(points)

        # Set all original points to red
        colors[:] = [1, 0, 0]  # Red for all original points
        colors[basal_indices] = [0, 1, 1]  # Set basal points that were used to cyan

        def process_basal_point(basal_point):
            # Calculate the direction vector from the basal point to the centroid
            direction_vector = centroid - basal_point
            direction_vector /= np.linalg.norm(direction_vector)  # Normalize

            # Step along the vector and find the first point in the point cloud that intersects with this direction
            steps = np.linspace(0.5, 2, 100)  # Reduced number of steps
            candidate_points = basal_point + steps[:, np.newaxis] * direction_vector

            # Query KD-Tree for nearest neighbors
            distances, indices = tree.query(candidate_points, k=1)

            # Find the first valid intersection
            mask = (distances < 0.05) & (distances > 1e-6)
            if np.any(mask):
                first_valid_idx = np.argmax(mask)
                opposite_point = points[indices[first_valid_idx]]

                # Generate new points between the basal point and the opposite point
                t = np.linspace(0, 1, n_points)[:, np.newaxis]
                new_points = basal_point + t * (opposite_point - basal_point)
                return new_points
            return None

        new_points = []
        with ThreadPoolExecutor() as executor:
            futures = [executor.submit(process_basal_point, bp) for bp in basal_points[:len(basal_points)//2]]
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    new_points.extend(result)

        # Add new points to the point cloud
        new_points = np.array(new_points)
        new_colors = np.full((new_points.shape[0], 3), [0, 0, 1])  # Blue color for new points

        # Add new points to the point cloud
        pcd.points = o3d.utility.Vector3dVector(np.vstack((points, new_points)))
        pcd.colors = o3d.utility.Vector3dVector(np.vstack((colors, new_colors)))

        return pcd

    @staticmethod
    def poisson_reconstruction(pcd):
        # Estimate normals
        pcd.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.1, max_nn=30)
        )

        # Orient normals consistently
        pcd.orient_normals_consistent_tangent_plane(k=30)

        # Get points and normals as numpy arrays
        points = np.asarray(pcd.points)
        normals = np.asarray(pcd.normals)

        # Calculate normal lengths
        normal_lengths = np.linalg.norm(normals, axis=1)

        # Create a mask for valid normals (non-zero length)
        valid_mask = normal_lengths > 1e-6

        # Filter out points with undefined or zero-length normals
        valid_points = points[valid_mask]
        valid_normals = normals[valid_mask]

        # Create a new point cloud with only valid points and normals
        valid_pcd = o3d.geometry.PointCloud()
        valid_pcd.points = o3d.utility.Vector3dVector(valid_points)
        valid_pcd.normals = o3d.utility.Vector3dVector(valid_normals)

        print(f"Original point cloud size: {len(points)}")
        print(f"Filtered point cloud size: {len(valid_points)}")

        # Invert normals
        pcd.normals = o3d.utility.Vector3dVector(-np.asarray(pcd.normals))

        # Apply Poisson surface reconstruction
        mesh, densities = mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
    pcd, depth=8, linear_fit=False
)

        return mesh
    
    def save_mesh(self):
        options = QFileDialog.Options()
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Save Mesh",
            "",
            "PLY Files (*.ply);;All Files (*)",
            options=options,
        )
        if file_name:
            if not file_name.lower().endswith(".ply"):
                file_name += ".ply"
            o3d.io.write_triangle_mesh(file_name, self.reconstructed_mesh)
            self.instructions_label.setText(f"Mesh saved to {file_name}")
            os.unlink(self.temp_mesh_path)

    def hide_buttons_except_next(self):
        """
        Hide all buttons except the 'Next' button.
        """
        self.load_button.setVisible(False)
        self.continue_button.setVisible(False)
        self.manual_selection_button.setVisible(False)
        self.continue_with_seeds_button.setVisible(False)
        self.next_button.setVisible(True)
        self.run_button.setVisible(False)

    def hide_buttons_for_poc(self):
        """
        Hide all buttons except those needed for point of contact selection.
        """
        self.load_button.setVisible(False)
        self.continue_button.setVisible(False)
        self.manual_selection_button.setVisible(False)
        self.continue_with_seeds_button.setVisible(False)
        self.next_button.setVisible(False)
        self.run_button.setVisible(False)
        self.input_poc_button.setVisible(False)
        self.save_pcd_button.setVisible(False)
        self.save_mesh_button.setVisible(False)
        self.reconstruct_mesh_button.setVisible(False)

    def hide_all_buttons(self):
        """
        Hide all buttons.
        """
        self.load_button.setVisible(False)
        self.continue_button.setVisible(False)
        self.manual_selection_button.setVisible(False)
        self.continue_with_seeds_button.setVisible(False)
        self.next_button.setVisible(False)
        self.run_button.setVisible(False)
        self.input_poc_button.setVisible(False)
        self.save_pcd_button.setVisible(False)
        self.reconstruct_mesh_button.setVisible(False)
        self.save_mesh_button.setVisible(False)
        self.slider_widget.setVisible(False)

    def closeEvent(self, event):
        """
        Handle the close event to terminate any running processes.
        """
        if self.process:
            self.process.terminate()
        super().closeEvent(event)


# Main entry point for the application
if __name__ == "__main__":
    # Set the start method for multiprocessing to 'spawn' (required for some platforms)
    multiprocessing.set_start_method("spawn")

    # Create the application instance and main window
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    # Start the application's event loop
    sys.exit(app.exec_())
