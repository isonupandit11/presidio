import os
import shutil
import copy
import tempfile
from pathlib import Path
from PIL import Image
import pydicom
from pydicom.pixel_data_handlers.util import apply_voi_lut
import PIL
import png
import numpy as np
from matplotlib import pyplot as plt  # necessary import for PIL typing # noqa: F401
from typing import Tuple, List, Union

import presidio_image_redactor
from presidio_image_redactor import ImageAnalyzerEngine
from presidio_analyzer import AnalyzerEngine, RecognizerRegistry, PatternRecognizer


class DicomImageRedactorEngine:
    """Performs OCR + PII detection + bounding box redaction.

    :param image_analyzer_engine: Engine which performs OCR + PII detection.
    """

    @staticmethod
    def _get_all_dcm_files(dcm_dir: Path) -> List[Path]:
        """Return paths to all DICOM files in a directory and its sub-directories.

        Args:
            dcm_dir (pathlib.Path): Path to a directory containing at least one .dcm file.

        Return:
            files (list): List of pathlib Path objects.
        """
        # Define applicable extensions (case-insensitive)
        extensions = ["dcm", "dicom"]

        # Get all files with any applicable extension
        all_files = []
        for extension in extensions:
            p = dcm_dir.glob(f"**/*.{extension}")
            files = [x for x in p if x.is_file()]
            all_files += files

        return all_files

    @staticmethod
    def _check_if_greyscale(instance: pydicom.dataset.FileDataset) -> bool:
        """Check if a DICOM image is in greyscale.

        Args:
            instance (pydicom.dataset.FileDataset): A single DICOM instance.

        Return:
            is_greyscale (bool): FALSE if the Photometric Interpolation is RGB.
        """
        # Check if image is grayscale or not using the Photometric Interpolation element
        color_scale = instance[0x0028, 0x0004].value
        is_greyscale = color_scale != "RGB"  # TODO: Make this more robust

        return is_greyscale

    @staticmethod
    def _rescale_dcm_pixel_array(
        instance: pydicom.dataset.FileDataset, is_greyscale: bool
    ) -> np.ndarray:
        """Rescale DICOM pixel_array.

        Args:
            instance (pydicom.dataset.FileDataset): a singe DICOM instance.
            is_greyscale (bool): FALSE if the Photometric Interpolation is RGB.

        Return:
            image_2d_scaled (numpy.ndarray): rescaled DICOM pixel_array.
        """
        # Normalize contrast
        if "WindowWidth" in instance:
            image_2d = apply_voi_lut(instance.pixel_array, instance)
        else:
            image_2d = instance.pixel_array

        # Convert to float to avoid overflow or underflow losses.
        image_2d_float = image_2d.astype(float)

        if not is_greyscale:
            image_2d_scaled = image_2d_float
        else:
            # Rescaling grey scale between 0-255
            image_2d_scaled = (
                np.maximum(image_2d_float, 0) / image_2d_float.max()
            ) * 255.0

        # Convert to uint
        image_2d_scaled = np.uint8(image_2d_scaled)

        return image_2d_scaled

    @classmethod
    def _convert_dcm_to_png(
        self, filepath: Path, output_dir: str = "temp_dir"
    ) -> tuple:
        """Convert DICOM image to PNG file.

        Args:
            filepath (pathlib.Path): Path to a single dcm file.
            output_dir (str): Path to output directory.

        Return:
            shape (tuple): Returns shape of pixel array.
            is_greyscale (bool): FALSE if the Photometric Interpolation is RGB.
        """
        ds = pydicom.dcmread(filepath)

        # Check if image is grayscale or not using the Photometric Interpolation element
        is_greyscale = self._check_if_greyscale(ds)

        image = self._rescale_dcm_pixel_array(ds, is_greyscale)
        shape = image.shape

        # Write the PNG file
        os.makedirs(output_dir, exist_ok=True)
        if is_greyscale:
            with open(f"{output_dir}/{filepath.stem}.png", "wb") as png_file:
                w = png.Writer(shape[1], shape[0], greyscale=True)
                w.write(png_file, image)
        else:
            with open(f"{output_dir}/{filepath.stem}.png", "wb") as png_file:
                w = png.Writer(shape[1], shape[0], greyscale=False)
                # Semi-flatten the pixel array to get RGB representation in two dimensions
                pixel_array = np.reshape(ds.pixel_array, (shape[0], shape[1] * 3))
                w.write(png_file, pixel_array)

        return shape, is_greyscale

    @staticmethod
    def _get_bg_color(
        image: PIL.PngImagePlugin.PngImageFile, is_greyscale: bool, invert: bool = False
    ) -> Union[int, Tuple[int, int, int]]:
        """Select most common color as background color.

        Args:
            image (PIL.PngImagePlugin.PngImageFile): Loaded PNG image.
            colorscale (str): Colorscale of image (e.g., 'grayscale', 'RGB')
            invert (bool): TRUE if you want to get the inverse of the bg color.

        Return:
            bg_color (tuple): Background color.
        """
        # Invert colors if invert flag is True
        if invert:
            if image.mode == "RGBA":
                # Handle transparency as needed
                r, g, b, a = image.split()
                rgb_image = Image.merge("RGB", (r, g, b))
                inverted_image = PIL.ImageOps.invert(rgb_image)
                r2, g2, b2 = inverted_image.split()

                image = Image.merge("RGBA", (r2, g2, b2, a))

            else:
                image = PIL.ImageOps.invert(image)

        # Get background color
        if is_greyscale:
            # Select most common color as color
            bg_color = int(np.bincount(list(image.getdata())).argmax())
        else:
            # Reduce size of image to 1 pixel to get dominant color
            tmp_image = image.copy()
            tmp_image = tmp_image.resize((1, 1), resample=0)
            bg_color = tmp_image.getpixel((0, 0))

        return bg_color

    @classmethod
    def _get_most_common_pixel_value(
        self, instance: pydicom.dataset.FileDataset, box_color_setting: str = "contrast"
    ) -> Union[int, Tuple[int, int, int]]:
        """Find the most common pixel value.

        Args:
            instance (pydicom.dataset.FileDataset): a singe DICOM instance.
            box_color_setting (str): Determines how box color is selected.
                'contrast' - Masks stand out relative to background.
                'background' - Masks are same color as background.

        Return:
            pixel_value (int or tuple of int): Most or least common pixel value
                (depending on box_color_setting).
        """
        # Get flattened pixel array
        flat_pixel_array = np.array(instance.pixel_array).flatten()

        is_greyscale = self._check_if_greyscale(instance)
        if is_greyscale:
            # Get most common value
            values, counts = np.unique(flat_pixel_array, return_counts=True)
            flat_pixel_array = np.array(flat_pixel_array)
            common_value = values[np.argmax(counts)]
        else:
            raise TypeError(
                "Most common pixel value retrieval is only supported for greyscale images at this point."  # noqa: E501
            )

        # Invert color as necessary
        if box_color_setting.lower() in ["contrast", "invert", "inverted", "inverse"]:
            pixel_value = np.max(flat_pixel_array) - common_value
        elif box_color_setting.lower() in ["background", "bg"]:
            pixel_value = common_value

        return pixel_value

    @classmethod
    def _add_padding(
        self,
        image: PIL.PngImagePlugin.PngImageFile,
        is_greyscale: bool,
        padding_width: int,
    ) -> PIL.PngImagePlugin.PngImageFile:
        """Add border to image using most common color.

        Args:
            image (PIL.PngImagePlugin.PngImageFile): Loaded PNG image.
            is_greyscale (bool): Whether image is in grayscale or not.
            padding_width (int): Pixel width of padding (uniform).

        Return:
            image_with_padding (PIL.PngImagePlugin.PngImageFile): PNG image with padding.
        """
        # Check padding width value
        if padding_width <= 0:
            raise ValueError("Enter a positive value for padding")
        elif padding_width >= 100:
            raise ValueError(
                "Excessive padding width entered. Please use a width under 100 pixels."
            )

        # Select most common color as border color
        border_color = self._get_bg_color(image, is_greyscale)

        # Add padding
        right = padding_width
        left = padding_width
        top = padding_width
        bottom = padding_width

        width, height = image.size

        new_width = width + right + left
        new_height = height + top + bottom

        image_with_padding = Image.new(
            image.mode, (new_width, new_height), border_color
        )
        image_with_padding.paste(image, (left, top))

        return image_with_padding

    @staticmethod
    def _copy_files_for_processing(src_path: str, dst_parent_dir: str) -> Path:
        """Copy DICOM files. All processing should be done on the copies.

        Args:
            src_path (str): Source DICOM file or directory containing DICOM files.
            dst_parent_dir (str): Parent directory of where you want to store the copies.

        Return:
            dst_path (pathlib.Path): Output location of the file(s).
        """
        # Identify output path
        tail = list(Path(src_path).parts)[-1]
        dst_path = Path(dst_parent_dir, tail)

        # Copy file(s)
        if Path(src_path).is_dir() is True:
            try:
                shutil.copytree(src_path, dst_path)
            except FileExistsError:
                raise FileExistsError(
                    "Destination files already exist. Please clear the destination files or specify a different dst_parent_dir."  # noqa: E501
                )
        elif Path(src_path).is_file() is True:
            # Create the output dir manually if working with a single file
            os.makedirs(Path(dst_path).parent, exist_ok=True)
            shutil.copy(src_path, dst_path)
        else:
            raise FileNotFoundError(f"{src_path} does not exist")

        return dst_path

    @staticmethod
    def _get_text_metadata(
        instance: pydicom.dataset.FileDataset,
    ) -> Tuple[list, list, list]:
        """Retrieve all text metadata from the DICOM image.

        Args:
            instance (pydicom.dataset.FileDataset): Loaded DICOM instance.

        Return:
            metadata_text (list): List of all the instance's element values
                (excluding pixel data).
            is_name (list): True if the element is specified as being a name.
            is_patient (list): True if the element is specified as being
                related to the patient.
        """
        metadata_text = list()
        is_name = list()
        is_patient = list()

        for element in instance:
            # Save all metadata except the DICOM image itself
            if element.name != "Pixel Data":
                # Save the metadata
                metadata_text.append(element.value)

                # Track whether this particular element is a name
                if "name" in element.name.lower():
                    is_name.append(True)
                else:
                    is_name.append(False)

                # Track whether this particular element is directly tied to the patient
                if "patient" in element.name.lower():
                    is_patient.append(True)
                else:
                    is_patient.append(False)
            else:
                metadata_text.append("")
                is_name.append(False)
                is_patient.append(False)

        return metadata_text, is_name, is_patient

    @staticmethod
    def _process_names(text_metadata: list, is_name: list) -> list:
        """Process names to have multiple iterations in our PHI list.

        Args:
            metadata_text (list): List of all the instance's element values
                (excluding pixel data).
            is_name (list): True if the element is specified as being a name.

        Return:
            phi_list (list): Metadata text with additional name iterations appended.
        """
        phi_list = text_metadata.copy()

        for i in range(0, len(text_metadata)):
            if is_name[i] is True:
                original_text = str(text_metadata[i])

                # Replacing separator character with space
                text_1 = original_text.replace("^", " ")

                # Capitalize all characters in name
                text_2 = text_1.upper()

                # Lowercase all characters in name
                text_3 = text_1.lower()

                # Capitalize first letter in each name
                text_4 = text_1.title()

                # Append iterations
                phi_list.append(text_1)
                phi_list.append(text_2)
                phi_list.append(text_3)
                phi_list.append(text_4)

                # Adding each name as a separate item in the list
                phi_list = phi_list + text_1.split(" ")
                phi_list = phi_list + text_2.split(" ")
                phi_list = phi_list + text_3.split(" ")
                phi_list = phi_list + text_4.split(" ")

        return phi_list

    @staticmethod
    def _add_known_generic_phi(phi_list: list) -> list:
        """Add known potential generic PHI values.

        Args:
            phi_list (list): List of PHI to use with Presidio ad-hoc recognizer.

        Return:
            phi_list (list): Same list with added known values.
        """
        phi_list.append("M")
        phi_list.append("[M]")
        phi_list.append("F")
        phi_list.append("[F]")
        phi_list.append("X")
        phi_list.append("[X]")
        phi_list.append("U")
        phi_list.append("[U]")

        return phi_list

    @classmethod
    def _make_phi_list(
        self,
        original_metadata: List[Union[pydicom.multival.MultiValue, list, tuple]],
        is_name: List[bool],
        is_patient: List[bool],
    ) -> list:
        """Make the list of PHI to use in Presidio ad-hoc recognizer.

        Args:
            original_metadata (list): List of all the instance's element values
                (excluding pixel data).
            is_name (list): True if the element is specified as being a name.
            is_patient (list): True if the element is specified as being
                related to the patient.

        Return:
            phi_str_list (list): List of PHI (str) to use with Presidio ad-hoc recognizer.
        """
        # Process names
        phi_list = self._process_names(original_metadata, is_name)

        # Add known potential phi values
        phi_list = self._add_known_generic_phi(phi_list)

        # Flatten any nested lists
        for phi in phi_list:
            if type(phi) in [pydicom.multival.MultiValue, list, tuple]:
                for item in phi:
                    phi_list.append(item)
                phi_list.remove(phi)

        # Convert all items to strings
        phi_str_list = [str(phi) for phi in phi_list]

        # Remove duplicates
        phi_str_list = list(set(phi_str_list))

        return phi_str_list

    @staticmethod
    def _create_custom_recognizer(
        phi_list: List[str],
    ) -> presidio_image_redactor.image_analyzer_engine.ImageAnalyzerEngine:
        """Create custom recognizer using DICOM metadata.

        Args:
            phi_list (list): List of PHI text pulled from the DICOM metadata.

        Return:
            custom_analyzer_engine (presidio_image_redactor.
                image_analyzer_engine.ImageAnalyzerEngine):
                Custom image analyzer engine.

        """
        # Create recognizer
        deny_list_recognizer = PatternRecognizer(
            supported_entity="PERSON", deny_list=phi_list
        )

        # Add recognizer to registry
        registry = RecognizerRegistry()
        registry.load_predefined_recognizers()
        registry.add_recognizer(deny_list_recognizer)

        # Create analyzer
        analyzer = AnalyzerEngine(registry=registry)
        custom_analyzer_engine = ImageAnalyzerEngine(analyzer_engine=analyzer)

        return custom_analyzer_engine

    @staticmethod
    def _get_bboxes_from_analyzer_results(analyzer_results: list) -> dict:
        """Organize bounding box info from analyzer results.

        Args:
            analyzer_results (list): Results from using ImageAnalyzerEngine.

        Return:
            bboxes_dict (dict): Bounding box info organized.
        """
        bboxes_dict = {}
        for i in range(0, len(analyzer_results)):
            result = analyzer_results[i].to_dict()

            bboxes_dict[str(i)] = {
                "entity_type": result["entity_type"],
                "score": result["score"],
                "left": result["left"],
                "top": result["top"],
                "width": result["width"],
                "height": result["height"],
            }

        return bboxes_dict

    @classmethod
    def _format_bboxes(self, analyzer_results: list, padding_width: int) -> list:
        """Format the bounding boxes to write directly back to DICOM pixel data.

        Args:
            analyzer_results (list): The analyzer results.
            padding_width (int): Pixel width used for padding (0 if no padding).

        Return:
            bboxes (list): Bounding box information per word.
        """
        if padding_width < 0:
            raise ValueError("Padding width must be a positive number.")

        # Write bounding box info to json files for now
        phi_bboxes_dict = self._get_bboxes_from_analyzer_results(analyzer_results)

        # convert detected bounding boxes to list
        bboxes = [phi_bboxes_dict[i] for i in phi_bboxes_dict.keys()]

        # remove padding from all bounding boxes
        bboxes = [
            {
                "top": max(0, bbox["top"] - padding_width),
                "left": max(0, bbox["left"] - padding_width),
                "width": bbox["width"],
                "height": bbox["height"],
            }
            for bbox in bboxes
        ]

        return bboxes

    @classmethod
    def _set_bbox_color(
        self, instance: pydicom.dataset.FileDataset, box_color_setting: str
    ):
        """Set the bounding box color.

        Args:
            instance (pydicom.dataset.FileDataset): A single DICOM instance.
            box_color_setting (str): Determines how box color is selected.
                'contrast' - Masks stand out relative to background.
                'background' - Masks are same color as background.

        Return:
            box_color (any): int or tuple of int values determining masking box color.
        """
        # Check if we want the box color to contrast with the background
        if box_color_setting.lower() in ["contrast", "invert", "inverted", "inverse"]:
            invert_flag = True
        elif box_color_setting.lower() in ["background", "bg"]:
            invert_flag = False
        else:
            raise ValueError("box_color_setting must be 'contrast' or 'background'")

        # Temporarily save as PNG to get color
        with tempfile.TemporaryDirectory() as tmpdirname:
            dst_path = Path(f"{tmpdirname}/temp.dcm")
            instance.save_as(dst_path)
            _, is_greyscale = self._convert_dcm_to_png(dst_path, output_dir=tmpdirname)

            png_filepath = f"{tmpdirname}/{dst_path.stem}.png"
            loaded_image = Image.open(png_filepath)
            box_color = self._get_bg_color(loaded_image, is_greyscale, invert_flag)

        return box_color

    @classmethod
    def _add_redact_box(
        self,
        instance: pydicom.dataset.FileDataset,
        bounding_boxes_coordinates: list,
        box_color_setting: str = "contrast",
    ) -> pydicom.dataset.FileDataset:
        """Add redaction bounding boxes on a DICOM instance.

        Args:
            instance (pydicom.dataset.FileDataset): A single DICOM instance.
            bounding_boxes_coordinates (dict): Bounding box coordinates.
            box_color_setting (str): Determines how box color is selected.
                'contrast' - Masks stand out relative to background.
                'background' - Masks are same color as background.

        Return:
            A new dicom instance with redaction bounding boxes.
        """

        # Copy instance
        redacted_instance = copy.deepcopy(instance)

        # Select masking box color
        is_greyscale = self._check_if_greyscale(instance)
        if is_greyscale:
            box_color = self._get_most_common_pixel_value(instance, box_color_setting)
        else:
            box_color = self._set_bbox_color(redacted_instance, box_color_setting)

        # Apply mask
        for i in range(0, len(bounding_boxes_coordinates)):
            bbox = bounding_boxes_coordinates[i]
            top = bbox["top"]
            left = bbox["left"]
            width = bbox["width"]
            height = bbox["height"]
            redacted_instance.pixel_array[
                top : top + height, left : left + width
            ] = box_color

        redacted_instance.PixelData = redacted_instance.pixel_array.tobytes()

        return redacted_instance

    @staticmethod
    def _validate_paths(input_path: str, output_dir: str) -> None:
        """Validate the DICOM path.

        Args:
            input_path (str): Path to input DICOM file or dir.
            output_dir (str): Path to parent directory to write output to.
        """
        # Check input is an actual file or dir
        if Path(input_path).is_dir() is False:
            if Path(input_path).is_file() is False:
                raise TypeError("input_path must be valid file or dir")

        # Check output is a directory
        if Path(output_dir).is_file() is True:
            raise TypeError(
                "output_dir must be a directory (does not need to exist yet)"
            )

    def _redact_single_dicom_image(
        self,
        dcm_path: str,
        box_color_setting: str,
        padding_width: int,
        overwrite: bool,
        dst_parent_dir: str,
    ) -> str:
        """Redact text PHI present on a DICOM image.

        Args:
            dcm_path (pathlib.Path): Path to the DICOM file.
            box_color_setting (str): Color setting to use for bounding boxes
                ("contrast" or "background").
            padding_width (int): Pixel width of padding (uniform).
            overwrite (bool): Only set to True if you are providing the
                duplicated DICOM path in dcm_path.
            dst_parent_dir (str): Parent directory of where you want to
                store the copies.

        Return:
            dst_path (str): Path to the output DICOM file.
        """
        # Ensure we are working on a single file
        if Path(dcm_path).is_dir():
            raise FileNotFoundError("Please ensure dcm_path is a single file")
        elif Path(dcm_path).is_file() is False:
            raise FileNotFoundError(f"{dcm_path} does not exist")

        # Copy file before processing if overwrite==False
        if overwrite is False:
            dst_path = self._copy_files_for_processing(dcm_path, dst_parent_dir)
        else:
            dst_path = dcm_path

        # Load instance
        instance = pydicom.dcmread(dst_path)

        # Load image for processing
        with tempfile.TemporaryDirectory() as tmpdirname:
            # Convert DICOM to PNG and add padding for OCR (during analysis)
            _, is_greyscale = self._convert_dcm_to_png(dst_path, output_dir=tmpdirname)
            png_filepath = f"{tmpdirname}/{dst_path.stem}.png"
            loaded_image = Image.open(png_filepath)
            image = self._add_padding(loaded_image, is_greyscale, padding_width)

        # Create custom recognizer using DICOM metadata
        original_metadata, is_name, is_patient = self._get_text_metadata(instance)
        phi_list = self._make_phi_list(original_metadata, is_name, is_patient)
        custom_analyzer_engine = self._create_custom_recognizer(phi_list)
        analyzer_results = custom_analyzer_engine.analyze(image)

        # Redact all bounding boxes from DICOM file
        bboxes = self._format_bboxes(analyzer_results, padding_width)
        redacted_dicom_instance = self._add_redact_box(
            instance, bboxes, box_color_setting
        )
        redacted_dicom_instance.save_as(dst_path)

        return dst_path

    def _redact_multiple_dicom_images(
        self,
        dcm_dir: str,
        box_color_setting: str,
        padding_width: int,
        overwrite: bool,
        dst_parent_dir: str,
    ) -> str:
        """Redact text PHI present on all DICOM images in a directory.

        Args:
            dcm_dir (str): Directory containing DICOM files (can be nested).
            box_color_setting (str): Color setting to use for bounding boxes
                ("contrast" or "background").
            padding_width (int): Pixel width of padding (uniform).
            overwrite (bool): Only set to True if you are providing
                the duplicated DICOM dir in dcm_dir.
            dst_parent_dir (str): Parent directory of where you want to
                store the copies.

        Return:
            dst_dir (str): Path to the output DICOM directory.
        """
        # Ensure we are working on a directory (can have sub-directories)
        if Path(dcm_dir).is_file():
            raise FileNotFoundError("Please ensure dcm_path is a directory")
        elif Path(dcm_dir).is_dir() is False:
            raise FileNotFoundError(f"{dcm_dir} does not exist")

        # List of files to process directly
        if overwrite is False:
            dst_dir = self._copy_files_for_processing(dcm_dir, dst_parent_dir)
        else:
            dst_dir = dcm_dir

        # Process each DICOM file directly
        all_dcm_files = self._get_all_dcm_files(Path(dst_dir))
        for dst_path in all_dcm_files:
            self._redact_single_dicom_image(
                dst_path, box_color_setting, padding_width, overwrite, dst_parent_dir
            )

        return dst_dir

    def redact(
        self,
        input_dicom_path: str,
        output_dir: str,
        padding_width: int = 25,
        box_color_setting: str = "contrast",
    ) -> None:
        """Redact method to redact the given image.

        Please notice, this method duplicates the image, creates a
        new instance and manipulate it.

        Args:
            input_dicom_path (str): Path to DICOM image(s).
            output_dir (str): Parent output directory.
            padding_width (int): Padding width to use when running OCR.
            box_color_setting (str): Color setting to use for redaction box
                ("contrast" or "background").
        """
        # Verify the given paths
        self._validate_paths(input_dicom_path, output_dir)

        # Create duplicate(s)
        dst_path = self._copy_files_for_processing(input_dicom_path, output_dir)

        # Process DICOM file(s)
        if Path(dst_path).is_dir() is False:
            output_location = self._redact_single_dicom_image(
                dcm_path=dst_path,
                box_color_setting=box_color_setting,
                padding_width=padding_width,
                overwrite=True,
                dst_parent_dir=".",
            )
        else:
            output_location = self._redact_multiple_dicom_images(
                dcm_dir=dst_path,
                box_color_setting=box_color_setting,
                padding_width=padding_width,
                overwrite=True,
                dst_parent_dir=".",
            )

        print(f"Output location: {output_location}")

        return None