# Copyright 2024 kubeflow.org.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import time

from kubeflow.storage_initializer.hugging_face import HuggingFaceDatasetParams
from kubeflow.storage_initializer.hugging_face import HuggingFaceModelParams
from kubeflow.storage_initializer.hugging_face import HuggingFaceTrainerParams
from kubeflow.training import constants
from kubeflow.training import TrainingClient
from kubeflow.training.utils import utils
from kubernetes import client
from kubernetes import config
from kubernetes.client.exceptions import ApiException
from peft import LoraConfig
import transformers

logging.basicConfig(format="%(message)s")
logging.getLogger("kubeflow.training.api.training_client").setLevel(logging.DEBUG)

TRAINING_CLIENT = TrainingClient(job_kind=constants.PYTORCHJOB_KIND)
JOB_NAME = "test-train-api"


def get_logs_of_master_pod(job_namespace, num_workers):
    # Verify that training job has correct pods.
    pod_names = TRAINING_CLIENT.get_job_pod_names(
        name=JOB_NAME, namespace=job_namespace
    )

    if len(pod_names) != num_workers:
        raise Exception(f"Training job has incorrect pods: {pod_names}")

    # Get and print the logs of the master pod.
    master_pod_name = next((name for name in pod_names if "master" in name), None)
    if master_pod_name:
        config.load_kube_config()  # Load kube config to interact with the cluster.
        v1 = client.CoreV1Api()
        try:
            pod_logs = v1.read_namespaced_pod_log(
                name=master_pod_name, namespace=job_namespace
            )
            logging.info(f"Logs of master pod {master_pod_name}:\n{pod_logs}")
        except ApiException as e:
            logging.error(f"Failed to get logs for pod {master_pod_name}: {e}")


def test_train_api(job_namespace):
    num_workers = 1

    # Use test case from fine-tuning API tutorial.
    # https://www.kubeflow.org/docs/components/training/user-guides/fine-tuning/
    TRAINING_CLIENT.train(
        name=JOB_NAME,
        namespace=job_namespace,
        # BERT model URI and type of Transformer to train it.
        model_provider_parameters=HuggingFaceModelParams(
            model_uri="hf://google-bert/bert-base-cased",
            transformer_type=transformers.AutoModelForSequenceClassification,
        ),
        # In order to save test time, use 8 samples from Yelp dataset.
        dataset_provider_parameters=HuggingFaceDatasetParams(
            repo_id="yelp_review_full",
            split="train[:8]",
        ),
        # Specify HuggingFace Trainer parameters. In this example, we will skip evaluation and model checkpoints.
        trainer_parameters=HuggingFaceTrainerParams(
            training_parameters=transformers.TrainingArguments(
                output_dir="test_trainer",
                save_strategy="no",
                evaluation_strategy="no",
                do_eval=False,
                disable_tqdm=True,
                log_level="info",
                num_train_epochs=1,
            ),
            # Set LoRA config to reduce number of trainable model parameters.
            lora_config=LoraConfig(
                r=8,
                lora_alpha=8,
                lora_dropout=0.1,
                bias="none",
            ),
        ),
        num_workers=num_workers,  # nodes parameter for torchrun command.
        num_procs_per_worker=1,  # nproc-per-node parameter for torchrun command.
        resources_per_worker={
            "gpu": 0,
            "cpu": 2,
            "memory": "10G",
        },
    )

    logging.info("---------------------------------------------------------------")
    logging.info(f"List of created {TRAINING_CLIENT.job_kind}s:")
    logging.info(TRAINING_CLIENT.list_jobs(job_namespace))

    logging.info("---------------------------------------------------------------")
    logging.info(f"Training job {JOB_NAME} is running...")

    logging.info("---------------------------------------------------------------")
    wait_timeout = 60 * 60
    polling_interval = 30
    for _ in range(round(wait_timeout / polling_interval)):

        # Get the list of pods associated with the job.
        pod_names = TRAINING_CLIENT.get_job_pod_names(
            name=JOB_NAME, namespace=job_namespace
        )

        config.load_kube_config()  # Load kube config to interact with the cluster.
        v1 = client.CoreV1Api()

        # Get Job only once per cycle and check the statuses.
        job = TRAINING_CLIENT.get_job(
            name=JOB_NAME,
            namespace=job_namespace,
            job_kind=constants.PYTORCHJOB_KIND,
            timeout=constants.DEFAULT_TIMEOUT,
        )

        for replica_name, replica_status in job.status.replica_statuses.items():
            logging.info(
                f"Replica {replica_name} status: {replica_status.succeeded} succeeded, {replica_status.failed} failed."
            )

        # Iterate over each pod to check its status.
        for pod_name in pod_names:
            pod_status = v1.read_namespaced_pod_status(
                name=pod_name, namespace=job_namespace
            )

            print("pod_status:")
            print(pod_status)
            print("pod_status.status:")
            print(pod_status.status)
            print("pod_status.status.container_statuses:")
            print(pod_status.status.container_statuses)
            print("continue...")

            # Ensure that container_statuses is not None before iterating.
            if pod_status.status.container_statuses is None:
                logging.warning(
                    f"Pod {pod_name} has no container statuses available yet."
                )
                continue

            # Check if any container in the pod has been restarted, indicating a previous failure.
            for container_status in pod_status.status.container_statuses:
                if container_status.restart_count > 0:
                    logging.warning(
                        f"Pod {pod_name} in job {JOB_NAME} has been restarted {container_status.restart_count} times. Retrieving logs..."
                    )

                    get_logs_of_master_pod(job_namespace, num_workers)

                    TRAINING_CLIENT.delete_job(JOB_NAME, job_namespace)

                    # Raise an exception to indicate that a pod has failed at least once.
                    raise Exception(f"Training job {JOB_NAME} is failed.")

        # Get Job conditions.
        conditions = TRAINING_CLIENT.get_job_conditions(
            job=job, timeout=constants.DEFAULT_TIMEOUT
        )

        # Check if the job has succeeded.
        if utils.has_condition(conditions, constants.JOB_CONDITION_SUCCEEDED):
            get_logs_of_master_pod(job_namespace, num_workers)
            logging.info(
                "---------------------------------------------------------------"
            )
            logging.info(f"Training job {JOB_NAME} is succeeded.")

            logging.info(
                "---------------------------------------------------------------"
            )
            TRAINING_CLIENT.delete_job(JOB_NAME, job_namespace)
            break

        time.sleep(polling_interval)


if __name__ == "__main__":
    test_train_api(job_namespace="default")
