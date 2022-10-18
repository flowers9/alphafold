# Copyright 2021 DeepMind Technologies Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Functions for building the input features for the AlphaFold model."""

import os
from typing import Any, Mapping, MutableMapping, Optional, Sequence, Tuple, Union
from absl import logging
from alphafold.common import residue_constants
from alphafold.data import msa_identifiers
from alphafold.data import parsers
from alphafold.data import templates
from alphafold.data.tools import hhblits
from alphafold.data.tools import hhsearch
from alphafold.data.tools import hmmsearch
from alphafold.data.tools import jackhmmer
import numpy as np
import concurrent.futures

# Internal import (7716).

FeatureDict = MutableMapping[str, np.ndarray]
TemplateSearcher = Union[hhsearch.HHSearch, hmmsearch.Hmmsearch]


def make_sequence_features(
    sequence: str, description: str, num_res: int) -> FeatureDict:
  """Constructs a feature dict of sequence features."""
  features = {}
  features['aatype'] = residue_constants.sequence_to_onehot(
      sequence=sequence,
      mapping=residue_constants.restype_order_with_x,
      map_unknown_to_x=True)
  features['between_segment_residues'] = np.zeros((num_res,), dtype=np.int32)
  features['domain_name'] = np.array([description.encode('utf-8')],
                                     dtype=np.object_)
  features['residue_index'] = np.array(range(num_res), dtype=np.int32)
  features['seq_length'] = np.array([num_res] * num_res, dtype=np.int32)
  features['sequence'] = np.array([sequence.encode('utf-8')], dtype=np.object_)
  return features


def make_msa_features(msas: Sequence[parsers.Msa]) -> FeatureDict:
  """Constructs a feature dict of MSA features."""
  if not msas:
    raise ValueError('At least one MSA must be provided.')

  int_msa = []
  deletion_matrix = []
  species_ids = []
  seen_sequences = set()
  for msa_index, msa in enumerate(msas):
    if not msa:
      raise ValueError(f'MSA {msa_index} must contain at least one sequence.')
    for sequence_index, sequence in enumerate(msa.sequences):
      if sequence in seen_sequences:
        continue
      seen_sequences.add(sequence)
      int_msa.append(
          [residue_constants.HHBLITS_AA_TO_ID[res] for res in sequence])
      deletion_matrix.append(msa.deletion_matrix[sequence_index])
      identifiers = msa_identifiers.get_identifiers(
          msa.descriptions[sequence_index])
      species_ids.append(identifiers.species_id.encode('utf-8'))

  num_res = len(msas[0].sequences[0])
  num_alignments = len(int_msa)
  features = {}
  features['deletion_matrix_int'] = np.array(deletion_matrix, dtype=np.int32)
  features['msa'] = np.array(int_msa, dtype=np.int32)
  features['num_alignments'] = np.array(
      [num_alignments] * num_res, dtype=np.int32)
  features['msa_species_identifiers'] = np.array(species_ids, dtype=np.object_)
  return features


def run_msa_tool(msa_runner, input_fasta_path: str, msa_out_path: str,
                 msa_format: str, use_precomputed_msas: bool,
                 max_sto_sequences: Optional[int] = None
                 ) -> Mapping[str, Any]:
  """Runs an MSA tool, checking if output already exists first."""
  if not use_precomputed_msas or not os.path.exists(msa_out_path):
    if msa_format == 'sto' and max_sto_sequences is not None:
      result = msa_runner.query(input_fasta_path, max_sto_sequences)[0]  # pytype: disable=wrong-arg-count
    else:
      result = msa_runner.query(input_fasta_path)[0]
    with open(msa_out_path, 'w') as f:
      f.write(result[msa_format])
  else:
    logging.warning('Reading MSA from file %s', msa_out_path)
    if msa_format == 'sto' and max_sto_sequences is not None:
      precomputed_msa = parsers.truncate_stockholm_msa(
          msa_out_path, max_sto_sequences)
      result = {'sto': precomputed_msa}
    else:
      with open(msa_out_path, 'r') as f:
        result = {msa_format: f.read()}
  return result


class DataPipeline:
  """Runs the alignment tools and assembles the input features."""

  def __init__(self,
               jackhmmer_binary_path: str,
               hhblits_binary_path: str,
               uniref90_database_path: str,
               mgnify_database_path: str,
               bfd_database_path: Optional[str],
               uniclust30_database_path: Optional[str],
               small_bfd_database_path: Optional[str],
               template_searcher: TemplateSearcher,
               template_featurizer: templates.TemplateHitFeaturizer,
               use_small_bfd: bool,
               mgnify_max_hits: int = 501,
               uniref_max_hits: int = 10000,
               hhblits_n_cpu: int = 4,
               jackhmmer_n_cpu: int = 8,
               parallel_execution: bool = False,
               use_precomputed_msas: bool = False):
    """Initializes the data pipeline."""
    self._use_small_bfd = use_small_bfd
    self.jackhmmer_uniref90_runner = jackhmmer.Jackhmmer(
        binary_path=jackhmmer_binary_path,
        n_cpu=jackhmmer_n_cpu,
        database_path=uniref90_database_path)
    if use_small_bfd:
      self.jackhmmer_small_bfd_runner = jackhmmer.Jackhmmer(
          binary_path=jackhmmer_binary_path,
          n_cpu=jackhmmer_n_cpu,
          database_path=small_bfd_database_path)
    else:
      self.hhblits_bfd_uniclust_runner = hhblits.HHBlits(
          binary_path=hhblits_binary_path,
          n_cpu=hhblits_n_cpu,
          databases=[bfd_database_path, uniclust30_database_path])
    self.jackhmmer_mgnify_runner = jackhmmer.Jackhmmer(
        binary_path=jackhmmer_binary_path,
        n_cpu=jackhmmer_n_cpu,
        database_path=mgnify_database_path)
    self.template_searcher = template_searcher
    self.template_featurizer = template_featurizer
    self.mgnify_max_hits = mgnify_max_hits
    self.uniref_max_hits = uniref_max_hits
    self.use_precomputed_msas = use_precomputed_msas
    self.parallel_execution = parallel_exceution

  def run_search_templates(self, input_sequence: str, msa_output_dir: str,
                           jackhmmer_uniref_90_result: str) -> templates.TemplateSearchResult:
    pdb_hits_out_path = os.path.join(
        msa_output_dir, f'pdb_hits.{self.template_searcher.output_format}')
    if not self.use_precomputed_msas or not os.path.exists(pdb_hits_out_path):
      msa_for_templates = jackhmmer_uniref90_result
      msa_for_templates = parsers.deduplicate_stockholm_msa(msa_for_templates)
      msa_for_templates = parsers.remove_empty_columns_from_stockholm_msa(
          msa_for_templates)
      if self.template_searcher.input_format == 'sto':
        pdb_templates_result = self.template_searcher.query(msa_for_templates)
      elif self.template_searcher.input_format == 'a3m':
        uniref90_msa_as_a3m = parsers.convert_stockholm_to_a3m(msa_for_templates)
        pdb_templates_result = self.template_searcher.query(uniref90_msa_as_a3m)
      else:
        raise ValueError('Unrecognized template input format: '
                         f'{self.template_searcher.input_format}')
      with open(pdb_hits_out_path, 'w') as f:
        f.write(pdb_templates_result)
    else:
      with open(pdb_hits_out_path, 'r') as f:
        pdb_templates_result = f.read()
    pdb_template_hits = self.template_searcher.get_template_hits(
        output_string=pdb_templates_result, input_sequence=input_sequence)
    templates_result = self.template_featurizer.get_templates(
        query_sequence=input_sequence,
        hits=pdb_template_hits)
    return templates_result

  # both runs jackhmmer_uniref90 and searches the resulting templates
  def run_jackhmmer_uniref90(self, input_fasta_path, msa_output_dir) -> Tuple[parsers.Msa, templates.TemplateSearchResult]:
    jackhmmer_uniref90_result = run_msa_tool(
        msa_runner=self.jackhmmer_uniref90_runner,
        input_fasta_path=input_fasta_path,
        msa_out_path=os.path.join(msa_output_dir, 'uniref90_hits.sto'),
        msa_format='sto',
        use_precomputed_msas=self.use_precomputed_msas,
        max_sto_sequences=self.uniref_max_hits)
    uniref90_msa = parsers.parse_stockholm(jackhmmer_uniref90_result['sto'])
    templates_result = run_search_templates(input_sequence, msa_output_dir, jackhmmer_uniref90_result['sto'])
    return uniref90_msa, templates_result

  def run_jackhmmer_mgnify(self, input_fasta_path, msa_output_dir) -> parsers.Msa:
    jackhmmer_mgnify_result = run_msa_tool(
        msa_runner=self.jackhmmer_mgnify_runner,
        input_fasta_path=input_fasta_path,
        msa_out_path=os.path.join(msa_output_dir, 'mgnify_hits.sto'),
        msa_format='sto',
        use_precomputed_msas=self.use_precomputed_msas,
        max_sto_sequences=self.mgnify_max_hits)
    mgnify_msa = parsers.parse_stockholm(jackhmmer_mgnify_result['sto'])
    return mgnify_msa

  def run_search_bfd(self, input_fasta_path, msa_output_dir) -> parsers.Msa:
    if self._use_small_bfd:
      jackhmmer_small_bfd_result = run_msa_tool(
          msa_runner=self.jackhmmer_small_bfd_runner,
          input_fasta_path=input_fasta_path,
          msa_out_path=os.path.join(msa_output_dir, 'small_bfd_hits.sto'),
          msa_format='sto',
          use_precomputed_msas=self.use_precomputed_msas)
      bfd_msa = parsers.parse_stockholm(jackhmmer_small_bfd_result['sto'])
    else:
      hhblits_bfd_uniclust_result = run_msa_tool(
          msa_runner=self.hhblits_bfd_uniclust_runner,
          input_fasta_path=input_fasta_path,
          msa_out_path=os.path.join(msa_output_dir, 'bfd_uniclust_hits.a3m'),
          msa_format='a3m',
          use_precomputed_msas=self.use_precomputed_msas)
      bfd_msa = parsers.parse_a3m(hhblits_bfd_uniclust_result['a3m'])
    return bfd_msa

  def process(self, input_fasta_path: str, msa_output_dir: str) -> FeatureDict:
    """Runs alignment tools on the input sequence and creates features."""
    with open(input_fasta_path) as f:
      input_fasta_str = f.read()
    input_seqs, input_descs = parsers.parse_fasta(input_fasta_str)
    if len(input_seqs) != 1:
      raise ValueError(
          f'More than one input sequence found in {input_fasta_path}.')
    input_sequence = input_seqs[0]
    input_description = input_descs[0]
    num_res = len(input_sequence)

    if self.parallel_execution:
      with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        uniref90_future = executor.submit(run_jackhmmer_uniref90, input_fasta_path, msa_output_dir)
        mgnify_future = executor.submit(run_jackhmmer_mgnify, input_fasta_path, msa_output_dir)
        bfd_future = executor.submit(run_search_bfd, input_fasta_path, msa_output_dir)
        uniref90_msa, templates_result = uniref90_future.result()
        mgnify_msa = mgnify_future.result()
        bfd_msa = bfd_future.result()
    else:
      uniref90_msa, templates_result = run_jackhmmer_uniref90(input_fasta_path, msa_output_dir)
      mgnify_msa = run_jackhmmer_mgnify(input_fasta_path, msa_output_dir)
      bfd_msa = run_search_bfd(input_fasta_path, msa_output_dir)

    sequence_features = make_sequence_features(
        sequence=input_sequence,
        description=input_description,
        num_res=num_res)

    msa_features = make_msa_features((uniref90_msa, bfd_msa, mgnify_msa))

    logging.info('Uniref90 MSA size: %d sequences.', len(uniref90_msa))
    logging.info('BFD MSA size: %d sequences.', len(bfd_msa))
    logging.info('MGnify MSA size: %d sequences.', len(mgnify_msa))
    logging.info('Final (deduplicated) MSA size: %d sequences.',
                 msa_features['num_alignments'][0])
    logging.info('Total number of templates (NB: this can include bad '
                 'templates and is later filtered to top 4): %d.',
                 templates_result.features['template_domain_names'].shape[0])

    return {**sequence_features, **msa_features, **templates_result.features}
