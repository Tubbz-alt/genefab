from sys import stderr
from urllib.request import urlopen
from json import loads
from os.path import join, isdir, isfile
from os import makedirs, remove, rename
from requests import get
from requests.exceptions import InvalidSchema
from urllib.error import URLError
from math import ceil
from tqdm import tqdm
from re import sub, search, IGNORECASE
from zipfile import ZipFile
from ._checks import safe_file_name

GENELAB_ROOT = "https://genelab-data.ndc.nasa.gov"
API_ROOT = "https://genelab-data.ndc.nasa.gov/genelab"


def get_json(url, verbose=False):
    """HTTP get, decode, parse"""
    if verbose:
        print("Parsing url: ", url, file=stderr)
    with urlopen(url) as response:
        return loads(response.read().decode())


def fetch_file(file_name, url, target_directory, update=False, verbose=False, http_fallback=True):
    """Perform checks, download file"""
    if not isdir(target_directory):
        if isfile(target_directory):
            raise OSError("Local storage exists and is not a directory")
        makedirs(target_directory)
    target_file = join(target_directory, file_name)
    if not update:
        if isdir(target_file):
            raise OSError("Directory with target name exists: " + target_file)
        if isfile(target_file):
            if verbose:
                print("Reusing", file_name, file=stderr)
            return target_file
    try:
        stream = get(url, stream=True)
    except InvalidSchema:
        if http_fallback:
            stream = get(sub(r'^ftp:\/\/', "http://", url), stream=True)
        else:
            raise
    if stream.status_code != 200:
        raise URLError("{}: status code {}".format(url, stream.status_code))
    total_bytes = int(stream.headers.get("content-length", 0))
    total_kb = ceil(total_bytes / 1024)
    with open(target_file, "wb") as output_handle:
        written_bytes = 0
        if verbose:
            stream_iterator = tqdm(
                stream.iter_content(1024), desc="Downloading "+file_name,
                total=total_kb, unit="KB"
            )
        else:
            stream_iterator = stream.iter_content(1024)
        for block in stream_iterator:
            output_handle.write(block)
            written_bytes += len(block)
    if total_bytes != written_bytes:
        remove(target_file)
        raise URLError("Failed to download the correct number of bytes")
    return target_file


def flat_extract(zip_filename, target_directory):
    """Extract zip file contents into a flat structure, with safety checks"""
    with ZipFile(zip_filename) as zf:
        for fileinfo in zf.filelist:
            if getattr(fileinfo, "file_size", 0): # is not a directory
                target_filename = safe_file_name(fileinfo.filename)
                zf.extract(fileinfo.filename, path=target_directory)
                rename(
                    join(target_directory, fileinfo.filename),
                    join(target_directory, target_filename)
                )


def permissive_search_group(expression, string, flags=IGNORECASE):
    """Like re.search(...).group(), but returns None if re.search() is None"""
    return getattr(
        search(expression, string, flags=flags),
        "group", lambda: None
    )()


FFIELD_VALUES = {
    "Project+Type": [
        "Spaceflight Study", "Spaceflight Project", "Spaceflight",
        "Flight Study", "Flight", "ground", "parabolic"
    ],
    "Study+Factor+Name": [
        "Absorbed Radiation Dose", "Age", "Altitude", "animal housing",
        "Antibiotic concentration", "Atmospheric Pressure", "Bed Rest",
        "Bleomycin Treatment", "cage", "CANONT:Part", "cell culture",
        "Cell cycle phase", "Cell Line", "clinical treatment", "collection set",
        "condition", "control group", "culture", "Culture Condition",
        "culture media", "development", "developmental condition",
        "developmental stage", "Diet", "dissection condition",
        "Dissection Timeline", "Donor", "dose", "ecotype", "EFO:light",
        "Electromagnetic Fields", "environment exposure",
        "Environmental Stress", "environmentalstress", "Exercise",
        "exposure duration", "food deprivation", "Fractionated Dose",
        "Freezing", "freezing profile", "Gender", "generation",
        "generation number", "genotype", "gravitation", "gravity",
        "gravity type", "Gravity, Altered", "growth environment",
        "hindlimb unloading", "hypergravity", "Individual", "infection",
        "Injection", "Ionizing Radiation", "Ionzing Radiation", "irradiate",
        "Irradiated", "Irradiation", "light cycle", "location",
        "Magnetic field", "MESH:Atmospheric Pressure", "MESH:Gravitation",
        "Microgravity", "mouse strain", "Muscle, Skeletal", "Neoplasm",
        "Nutrition", "O2", "organism part", "organism_part", "osteo-induced",
        "post radiation timepoint", "Preservation method", "pressure",
        "protocol host", "protocoltype", "Radiation", "Radiation Distance",
        "Radiation dosage", "radiation dose", "radiation type",
        "Radiation, Ionzing", "RNA sequencing", "sample collection protocol",
        "sample type", "Sampling time", "Sex", "Simulated microgravity",
        "Smoking Status", "Space Flight", "spaceflight",
        "Stimulated gravity (g level)", "stimulus", "strain", "strain/genotype",
        "Stress", "target gene specification", "temperature", "time",
        "time after treatment", "timepoint", "tissue", "tissue storage time",
        "treated with", "Treatment", "Treatment Duration", "treatment group",
        "treatment time", "variation", "viral load", "water deprivation",
        "weightlessness", "Weightlessness Simulation", "zygosity"
    ],
    "organism": [
        "Acinetobacter pittii", "Arabidopsis thaliana", "Aspergillus fumigatus",
        "Aspergillus niger", "Aspergillus terreus", "Aureobasidium pullulans",
        "Bacillus", "Bacillus subtilis", "Beauveria bassiana", "Brassica rapa",
        "Caenorhabditis elegans", "Candida albicans", "cellular organisms",
        "Ceratopteris richardii", "Cladosporium cladosporioides",
        "Cladosporium sphaerospermum", "Danio rerio", "Daphnia magna",
        "Drosophila melanogaster", "Enterobacter",
        "Enterobacteria phage lambda", "environmental samples",
        "Escherichia coli", "Euprymna scolopes", "Fusarium solani",
        "Helix lucorum", "Homo sapiens", "Klebsiella", "metagenomic data",
        "Microbiota", "Mus musculus", "Mycobacterium marinum",
        "Oryzias latipes", "Pantoea conspicua", "Pseudomonas aeruginosa",
        "Rattus norvegicus", "Rhodospirillum rubrum",
        "Saccharomyces cerevisiae", "Staphylococcus", "Staphylococcus aureus",
        "Streptococcus mutans", "Trichoderma virens"
    ],
    "Study+Assay+Measurement+Type": [
        "deletion pool profiling", "DNA methylation profiling",
        "environmental gene survey", "genome sequencing",
        "metabolite profiling", "protein expression profiling",
        "RNA methylation profiling", "transcription profiling"
    ]
}


FFIELD_ALIASES = {
    "ptype": "Project+Type",
    "factor": "Study+Factor+Name",
    "organism": "organism",
    "assay": "Study+Assay+Measurement+Type"
}
