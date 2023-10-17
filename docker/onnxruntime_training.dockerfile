# Copyright 2023 The HuggingFace Team All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Use nvidia/cuda image
FROM nvidia/cuda:11.8.0-cudnn8-devel-ubuntu20.04

# Ignore interactive questions during `docker build`
ENV DEBIAN_FRONTEND noninteractive

# Bash shell
RUN chsh -s /bin/bash
SHELL ["/bin/bash", "-c"]

# Versions
ARG PYTHON_VERSION=3.9
ARG TORCH_CUDA_VERSION=cu118
ARG TORCH_VERSION=2.0.0
ARG TORCHVISION_VERSION=0.15.1

# Install and update tools to minimize security vulnerabilities
RUN apt-get update
RUN apt-get install -y software-properties-common wget apt-utils patchelf git libprotobuf-dev protobuf-compiler cmake \
    bzip2 ca-certificates libglib2.0-0 libxext6 libsm6 libxrender1 mercurial subversion libopenmpi-dev && \
    apt-get clean
RUN unattended-upgrade
RUN apt-get autoremove -y

# Install miniconda (comes with python 3.9 default)
ARG BUILD_USER=onnxruntimedev
ARG MINICONDA_PREFIX=/home/$BUILD_USER/miniconda3
RUN apt-get install curl

ARG CONDA_URL=https://repo.anaconda.com/miniconda/Miniconda3-py37_4.9.2-Linux-x86_64.sh
RUN curl -fSsL --insecure ${CONDA_URL} -o install-conda.sh && \
    /bin/bash ./install-conda.sh -b -p $MINICONDA_PREFIX && \
    $MINICONDA_PREFIX/bin/conda clean -ya && \
    $MINICONDA_PREFIX/bin/conda install -y python=${PYTHON_VERSION}

ENV PATH=$MINICONDA_PREFIX/bin:${PATH}

ARG PYTHON_EXE=$MINICONDA_PREFIX/bin/python

# PyTorch
RUN $PYTHON_EXE -m pip install onnx ninja
RUN $PYTHON_EXE -m pip install torch==${TORCH_VERSION} torchvision==${TORCHVISION_VERSION} -f https://download.pytorch.org/whl/${TORCH_CUDA_VERSION}

# ORT Module
RUN $PYTHON_EXE -m pip install onnxruntime-training==1.15.1 -f https://download.onnxruntime.ai/onnxruntime_stable_cu118.html
RUN $PYTHON_EXE -m pip install torch-ort
ENV TORCH_CUDA_ARCH_LIST="5.2 6.0 6.1 7.0 7.5 8.0 8.6+PTX"
RUN $PYTHON_EXE -m pip install --upgrade protobuf==3.20.2
RUN $PYTHON_EXE -m torch_ort.configure
